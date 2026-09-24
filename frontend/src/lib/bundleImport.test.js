/**
 * @vitest-environment jsdom
 *
 * The Data tab's half of M4.14: the import that outlives its own request, and the button that
 * stopped re-arming. Spec v2.1 §10 (swap sequence), §6.6 (Data tab), §5.3 (the import is a job),
 * §3.1 (bundle-less is legal); decisions 253, 254, 257 and 258; findings 2.1, 2.3, 2.17, 2.22.
 *
 * MOUNTED AND UNIT-TESTED RATHER THAN LEFT TO PLAYWRIGHT, for the reason decision 226 admits a
 * vitest id beside a Playwright one. Every state this file asserts is a state the browser suite
 * cannot reach twice: the destructive button's rule has six phases, three of which exist only
 * while a 127 s import is in flight in the worker; the poll's deadline is eleven minutes long;
 * and a broken install is a database row whose files someone deleted, which `e2e/run.mjs` would
 * have to produce and then repair for every spec that follows it in a filename-ordered,
 * single-worker suite. The ids here are named BESIDE the `test_bundle_import_job.py` and
 * `01-first-boot.spec.js` ids on the same rows, never instead of them.
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api.js', () => ({ api: vi.fn(), get: vi.fn(), post: vi.fn() }));

import { get, post } from '$lib/api.js';
import BundleImport from './components/BundleImport.svelte';
import DataPage from '../routes/admin/data/+page.svelte';
import { session } from './session.svelte.js';
import {
  FAILED,
  IDLE,
  IMPORTED,
  POLL_DEADLINE_MS,
  POLL_INTERVAL_MS,
  POLL_READ_TIMEOUT_MS,
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

/** `GET /api/admin/bundle/state`, every key the Data tab reads. */
const bundleState = (over = {}) => ({
  bundles: [],
  active: null,
  loaded: null,
  restart_required: false,
  broken: false,
  missing_path: null,
  import_dir: '/data/import',
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

// Named by its idle label, which is also the only state it can be pressed in: `run()` swaps it
// to 'Working...' while a request of this screen's own is in flight.
const validateButton = () =>
  [...target.querySelectorAll('button')].find((b) => b.textContent.includes('Validate bundle'));

const box = () => target.querySelector('.box');
const lit = () => target.querySelectorAll('.step.on').length;

// ---------------------------------------------------------------- the button's one rule

describe('the import button', () => {
  // Six phases, one per case, because the old predicate was wrong in three of them and a
  // template is not a place a rule can be asserted. [M4.14 finding 2.3]
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
    // The state M4.14 created and fe-lc-11's published predicate could not see: the 202 landed,
    // the operator is looking at an ok report, and a second press would race the first import
    // for section 10's staging tree.
    expect(importDisabled({ phase: RUNNING, report: report() })).toBe(true);
  });

  it('stays dark once the bundle is imported', () => {
    expect(importDisabled({ phase: IMPORTED, report: report() })).toBe(true);
  });

  it('stays dark after a failed import', () => {
    expect(importDisabled({ phase: FAILED, report: report({ ok: false }) })).toBe(true);
    // The shape the defect was measured in: a transport failure leaves `report` null, and
    // `null && !null.ok` is falsy, so the old rule forbade nothing here.
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

// ---------------------------------------------------------------- the poll

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
    // Converting a lost GET into "it failed" is finding 2.1's own defect one layer up: the
    // operator told an import failed while it completed and flipped.
    const read = reader(
      new Error('Failed to fetch'),
      bundleState({ import_job: job({ phase: 'active', ok: true, report: report() }) })
    );
    const outcome = await pollImportJob(read, 7, noSleep);
    expect(outcome.phase).toBe(IMPORTED);
    expect(outcome.error).toBe('');
  });

  it("gives up at its own deadline, because the api client sets none", async () => {
    // `api.js` passes no timeout to `fetch` and M4.14 does not touch it, so without this the
    // page spins for as long as the tab is open. The clock is injected; a test that waited
    // eleven real minutes to prove a deadline is a test nobody runs.
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
    // The row it gives up on is still THIS import's row: `UNKNOWN` is "this page cannot say",
    // not "there was nothing to see", and the operator's own job is what the panel may show.
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
    // The other half of the same rule, and the half a phase assertion cannot carry: the foreign
    // row is not handed back either. `BundleImport` renders `outcome.job.report` as this
    // screen's verdict with no id test of its own, so a row that matched nothing here is
    // somebody else's migration report printed under this page's "outcome unknown" banner --
    // which is the thing the comment beside the `job_id` test says must not happen.
    // [M4.14 review cycle 1, waveE-03]
    expect(outcome.job).toBe(null);
  });

  it('answers for the row it was given, with no off-switch on the way it is chosen', async () => {
    // The guard above had two disjuncts that turned itself off when the id was absent, reachable
    // by no caller and exercised by no test -- and the one future caller they invited is the
    // lost-202 recovery, "poll without an id and adopt whatever is running", which is precisely
    // somebody else's stored report printed under this screen's banner. A guard with a documented
    // off-switch that nothing tests is a guard one edit from being off.
    // [M4.14 cycle 4, m414-c4-dim202-06]
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
    // Svelte destroying a component does not stop an async function it started, so without this
    // the poll kept reading for the rest of its eleven minutes and then wrote a phase and fired
    // `onImported` into a screen nobody is looking at. `null` is the absence of an outcome: a
    // caller that was told to stop has nothing to render. [M4.14 cycle 4, m414-c4-dim202-02]
    let clock = 0;
    let gone = false;
    const read = reader(bundleState({ import_job: job({ phase: 'running' }) }));
    // The deadline is injected as well as the screen's departure, so that a poll which ignored
    // the departure ends at its own deadline and fails this case rather than spinning it.
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
    // One read, and then the sleep that ends it: the loop does not spend another request on a
    // screen that has been destroyed.
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
    // 422 (the report says no), 409 (already running), 400 (the path) are answers: nothing was
    // enqueued. A 502, a 504 and a fetch that threw are not.
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
    // A refusal with no report at all -- a path outside DATA_DIR, a file that is not an archive
    // -- must not light the step named "report".
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
    // `importer/report._detail_value`: five items and the total, so the two records of one
    // import cannot describe it differently and the count survives the truncation.
    expect(detailLine([1, 2, 3, 4, 5, 6, 7])).toBe('1, 2, 3, 4, 5, ... (7 total)');
    expect(detailLine(42)).toBe('42');
    expect(detailLine('x'.repeat(120)).endsWith('...')).toBe(true);
    expect(detailLine('x'.repeat(120)).length).toBe(96);
  });
});

// ---------------------------------------------------------------- the component

describe("the Data tab's import control", () => {
  const open = async () => {
    const app = mount(BundleImport, { target, props: {} });
    await settle();
    return app;
  };

  it('renders the report the worker stored rather than the one the request returned', async () => {
    // The 202 carries the VALIDATION report -- nothing has been imported when it is written --
    // and section 10's migration report is the one the worker produces. Decision 253 stores it
    // on the `job_run` row, which is why this screen polls for it. [M4.14 step E4, finding 2.1]
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
      // The options carry the per-read deadline the poll gained in cycle 4 (dim202-03); what
      // this line is about is the endpoint the report came from.
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
    // Initialised rather than bare `let release;` so `svelte-check` can see it is callable; the
    // poll's first read is immediate, so this promise is what holds the component in `running`.
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
    // The reproduction: `catch` sets `report = err.detail?.report ?? null`, and the old rule
    // read `report && !report.ok`, so a 502, a proxy cut or a browser that gave up on a fetch
    // `api.js` never bounded left the one destructive control on this screen live -- at the one
    // moment nobody knows whether a `job_run` row was inserted on the way out.
    // [M4.14 findings 2.1 and 2.3]
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
      // The validation report stays: it is what the operator pressed Import on, and the strip
      // stops where this page stopped watching.
      expect(target.querySelector('.verdict').textContent).toBe('valid');
      expect(lit(), 'no swap was watched, so the strip does not claim one').toBe(2);
    } finally {
      unmount(app);
    }
  });

  it('says why a refusal refused, even with a report still on screen', async () => {
    // The old error line was `{#if error && !report}`, so a 409 arriving after a successful
    // validate -- which is exactly decision 253's "another bundle import is already running" --
    // printed nothing at all.
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
    // Decision 257: a directory holding exactly one `.tar` / `.tar.zst` is opened as that
    // archive, which is how the corpus bundle arrives on the box.
    const app = await open();
    try {
      expect(target.querySelector('label').textContent).toContain('.TAR/.TAR.ZST');
    } finally {
      unmount(app);
    }
  });
});

// ---------------------------------------------------------------- the page's two banners

describe('the Data tab', () => {
  const pageState = (over = {}) => bundleState({ active: 'test-v1', bundles: [], ...over });

  const openPage = async (state) => {
    vi.mocked(get).mockImplementation(async (path) => {
      if (path === '/admin/bundle/state') return state;
      throw new Error('no sources in this fixture');
    });
    const app = mount(DataPage, { target, props: {} });
    await settle();
    return app;
  };

  const warnings = () => [...target.querySelectorAll('.warn')].map((w) => w.textContent);

  it('renders the restore instruction rather than the restart one when the directory is gone', async () => {
    // Decision 258: `restart_required` is now `active != loaded AND NOT broken`, so the two
    // banners are two instructions that can never render together -- a restart reloads the same
    // empty store for an install whose files are gone, which is what dd01 measured. The restore
    // line owes section 2's action and, now that D2 exists, the repair the state had none of.
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
    // The boundary of the same change: a swap that has not been loaded yet is the state the
    // restart banner is for, and it must still say so.
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
    // M4.14 made the import outlive the request that queued it, so `/admin/bundle/state` can
    // answer `running` on a page load that pressed nothing: an operator who reloaded during the
    // 127 s load, or who opened the Data tab on a second device. Nothing on the page read
    // `import_job` -- the payload was grown in this milestone for exactly this screen -- so that
    // operator got an idle wizard and an armable Validate, on the one page section 6.6 makes
    // their instrument. [M4.14 review cycle 1, waveE-06]
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
      // `/config`, `/setup/state`, `/auth/me`: the adopted import lands, which calls
      // `onImported`, which bootstraps the shell exactly as this tab's own import would.
      return {};
    });
    const app = mount(DataPage, { target, props: {} });
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
    // The other half of the adoption path. `watch()` sets `phase = RUNNING` and never touches
    // `busy`, so the sibling button -- `disabled={busy}` and nothing else -- stayed live for the
    // whole of an import this tab was only watching. A press was not harmless:
    // `/admin/bundle/validate` takes no lock and the import is one uncommitted transaction, so it
    // answers ok, `run('validate')` writes VALIDATED over the running phase, and "Import and
    // activate" arms in the state E4 calls terminal-until-polled -- the strip dropping from three
    // lit steps to two while the worker is mid-swap, and a 409 refusal printed over an import that
    // is succeeding. A watch in flight is this screen's one writer of `phase`.
    // [M4.14 cycle 2, m414-c2-waveE-03]
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
    const app = mount(DataPage, { target, props: {} });
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
    // The boundary, and why the rule is a phase test and not a null test: the newest `job_run`
    // row outlives the import forever, so a page that adopted any row at all would re-poll,
    // re-fire `onImported` and re-bootstrap the shell on every visit to the Data tab for the
    // life of the install. [M4.14 review cycle 1, waveE-06]
    const app = await openPage(
      pageState({ import_job: job({ phase: 'active', ok: true, report: report() }) })
    );
    try {
      expect(box().getAttribute('data-phase')).toBe(IDLE);
      expect(target.querySelector('.report'), 'and no stored report is resurrected').toBe(null);
      // Counted on the page's own two routes: M5.6's four cards below the importer each read
      // their own, and a poll is a second read of the state route, which is what this holds.
      const bundleReads = vi
        .mocked(get)
        .mock.calls.filter(([path]) => path === '/admin/bundle/state' || path === '/admin/data/sources');
      expect(bundleReads, 'state and sources, and no poll').toHaveLength(2);
    } finally {
      unmount(app);
    }
  });

  it('leaves no poll behind when the operator walks away mid-import', async () => {
    // The Data tab is one tab of five and a 127 s import is long enough to leave: the operator
    // clicks Connectors and comes back. SvelteKit destroys this page and builds it again, and an
    // async poll survives both -- so the reads doubled per visit, and every live poll fired
    // `onImported` independently when the row flipped, which on this page re-bootstraps the
    // shared session store over whatever screen they are now looking at.
    // [M4.14 cycle 4, m414-c4-dim202-02]
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
    // The read that was in flight when the screen went away, answering with the terminal phase:
    // the one moment the abandoned poll used to write a phase and call back.
    release(bundleState({ import_job: job({ phase: 'active', ok: true, report: report() }) }));
    await settle();
    expect(onImported, 'a destroyed screen does not re-bootstrap the shell').not.toHaveBeenCalled();
    expect(vi.mocked(get), 'and it asks nothing further').toHaveBeenCalledTimes(1);
  });

  it('bounds each read of the state route, which the api client does not', async () => {
    // `api.js` passes `opts.signal` to `fetch` and sets no timeout; the poll's own deadline is
    // tested between reads, so a request that never settles suspends it for as long as the tab
    // is open. The signal is the whole assertion: an aborted read throws, which the poll already
    // records as an error and carries on from. [M4.14 cycle 4, m414-c4-dim202-03]
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
      // Inside the poll's own budget and outside one interval: a read may not outlive the poll
      // that owns it, and must not be cut off while the next tick is already due.
      expect(POLL_READ_TIMEOUT_MS).toBeGreaterThan(POLL_INTERVAL_MS);
      expect(POLL_READ_TIMEOUT_MS).toBeLessThan(POLL_DEADLINE_MS);
    } finally {
      unmount(app);
    }
  });
});

// ---------------------------------------------------------------- section 3.1's wizard

describe("the first-boot wizard's importer", () => {
  // §3.1 mounts this component as `<BundleImport onImported={...} />` -- no row handed down,
  // because the wizard reads `/setup/state` and never `/admin/bundle/state`. First boot is the
  // one place the measured 127 s import actually happens, so it is the one surface that has to
  // survive §2's 100 s proxy cut and an F5, and it was the one that could not: step 3 reopened
  // idle with Validate armed over a worker mid-swap, and the press that followed painted a 409
  // over an import that was succeeding. [M4.14 cycle 4, m414-c4-dim202-01]
  afterEach(() => {
    session.user = null;
  });

  it('adopts a running import nobody handed it, the way the Data tab does', async () => {
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
    // Step 0 of the wizard is reachable signed out and its progress dots reach step 3, so a
    // passer-by mounts this component. `/admin/bundle/state` is `AdminUser`-gated: a read this
    // visitor did not ask for buys a 401 on the one screen §3.1 gives them, and nothing to adopt.
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
    // The read this screen does for itself is a round trip, and Finish, Back or a reload can
    // land inside it: `onDestroy` has already had its one chance to stop a poll by the time the
    // answer arrives, so the decision to start one has to be taken again after the await.
    // [M4.14 cycle 4, m414-c4-dim202-01 and -02]
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
    // A read nobody asked for cannot become an error banner about itself: there is no import to
    // adopt either way, and the screen's idle state is the truth it already had.
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
