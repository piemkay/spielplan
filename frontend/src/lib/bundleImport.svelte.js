/**
 * The bundle import's phase machine, and the one rule that arms its destructive button.
 * Spec v2.1 §10 (the swap sequence), §6.6 (the Data tab), §5.3 (the import is a job with a
 * minutes budget); decisions 253 and 258; M4.14 findings 2.1 and 2.3.
 *
 * WHY THIS IS A MODULE AND NOT TWENTY LINES INSIDE `BundleImport.svelte`. The rule that decides
 * whether "Import and activate" is pressable is the only thing standing between an operator and
 * a second content seed, and it was written inline as
 * `disabled={busy || phase === 'idle' || (report && !report.ok)}`. `report` is `null` on every
 * transport failure — `err.detail?.report ?? null` — and `null && ...` is falsy, so the button
 * came back to life the instant a request failed, which is the one moment nobody knows what the
 * install is doing. A predicate in a template is a predicate nothing can assert: Playwright can
 * click the button in the state the browser happens to be in, and this rule has six states, three
 * of which only exist for the seconds while a 127 s import is in flight. [M4.14 finding 2.3]
 *
 * PURE FUNCTIONS, NOT MODULE `$state`. The shell modules beside this one (`rail.svelte.js`,
 * `session.svelte.js`) hold one object because they describe one thing that is true of the whole
 * app. An import is not: §3.1's wizard step 3 and §6.6's Data tab mount the SAME component
 * ("the same importer"), and module state would make one screen's phase the other's. The
 * component keeps its runes; this module keeps the rules, which is the half worth testing.
 *
 * THE PHASE NAMES ARE THIS SCREEN'S, and `phaseOfJob` is where they meet the server's. `job_run`
 * calls a finished import `active` because that is what happened to the `artifact_bundle` row
 * (`worker.PHASE_ACTIVE`); this screen calls it `imported`, which is what happened to the
 * operator, and still owes them §10's restart. Keeping the component's own vocabulary also keeps
 * `phase === 'imported'` — the restart banner's condition since M0 — meaning what it always did.
 */

/** Nothing has been validated yet. */
export const IDLE = 'idle';
/** A validation report is on screen; only an `ok` one arms the import. */
export const VALIDATED = 'validated';
/** The 202 landed and a `job_run` row is queued or running in the worker. */
export const RUNNING = 'running';
/** The worker finished and flipped the row; §10's restart is still owed. */
export const IMPORTED = 'imported';
/** The import was refused, or it ran and failed. A stored report says which. */
export const FAILED = 'failed';
/** Nothing on this page can say what the install is doing. The honest sixth state. */
export const UNKNOWN = 'unknown';

/**
 * What the page says in `UNKNOWN`, and the reason the button stays dark.
 *
 * It names the next action rather than apologising: after M4.14 the import outlives the request
 * that started it, so a request this page lost track of may well be flipping the row right now,
 * and `GET /api/admin/bundle/state` is the one place that knows. ASCII hyphen — this string is
 * also what a failing vitest assertion prints on a cp1252 console (CLAUDE.md).
 */
export const UNKNOWN_OUTCOME = 'outcome unknown - check the bundle state before retrying';

/**
 * How often the Data tab asks `/admin/bundle/state` while an import is in flight.
 *
 * Two seconds against a job the worker claims within one `TICK_SECONDS` (20 s) and then holds
 * for ~127 s on the real bundle: fast enough that the terminal phase reaches the screen while
 * the operator is still looking at it, slow enough that a two-minute import costs the backend
 * about sixty small reads of one `job_run` row and one `artifact_bundle` scan.
 */
export const POLL_INTERVAL_MS = 2_000;

/**
 * When the page stops asking and admits it does not know. §5.3's "minutes", bounded.
 *
 * `api.js` sets no timeout and this milestone must not change it, so a poll with no deadline of
 * its own is a spinner that can outlive the session. The number is twice `worker`'s own ceiling
 * for one attempt (`BUNDLE_IMPORT_TIMEOUT`, 300 s) plus room for the claim: past that the row is
 * terminal either way — the job's own `wait_for` closed it, or `_reap_abandoned_import` did —
 * so a page still reading `running` here is a page whose reads are not arriving, which is
 * exactly `UNKNOWN` and not `FAILED`.
 */
export const POLL_DEADLINE_MS = 660_000;

/**
 * How long one read of `/admin/bundle/state` may hang before the poll gives up on that read.
 *
 * The deadline above bounds the interval BETWEEN reads — it is tested once a read has returned
 * or thrown — so a request that never settles never reaches it, and the page sits on `running`
 * for as long as the tab is open rather than reaching `UNKNOWN_OUTCOME`, which is the one line
 * that sends the operator to go and read the bundle state. `api.js` hands `opts.signal` to
 * `fetch` and deliberately sets no timeout of its own, so the bound belongs to the caller that
 * knows what it is waiting for. Five intervals: longer than any answer a backend busy with a
 * 127 s import owes this screen, short enough that the deadline above is reached in minutes.
 * [M4.14 cycle 4, m414-c4-dim202-03]
 */
export const POLL_READ_TIMEOUT_MS = POLL_INTERVAL_MS * 5;

// The server's `job_run.detail->>'phase'` (`worker.PHASE_*`, `api/artifacts.QUEUED`) in this
// screen's vocabulary. `queued` and `running` are one phase here on purpose: the Data tab's
// question is "may I press anything?", and the answer is the same for both.
const PHASE_OF_JOB = {
  queued: RUNNING,
  running: RUNNING,
  active: IMPORTED,
  failed: FAILED
};

/**
 * `GET /api/admin/bundle/state`'s `import_job`, in this screen's phase names.
 *
 * `null` for "there is no import to report", which is an install that has never imported through
 * the route — distinct from `UNKNOWN`, which is this page failing to find out. An unrecognised
 * phase string is `UNKNOWN` rather than ignored: a worker that learns a new phase this build has
 * never heard of must not read as a finished import.
 */
export function phaseOfJob(job) {
  if (!job) return null;
  return PHASE_OF_JOB[job.phase] ?? UNKNOWN;
}

/**
 * The destructive button's one rule: enabled only on a positive report, and never in a state
 * whose outcome is unknown.
 *
 * Stated as what it permits rather than as a list of what it forbids, which is what made the old
 * predicate wrong twice over. `busy || phase === 'idle' || (report && !report.ok)` forbade three
 * things and permitted everything else, so it armed the button after a failed request (no
 * report), after a finished import (nothing left to import), and — once M4.14 made the import a
 * job — for the whole minute the load is running in the worker, which is the one window where a
 * second press means two imports racing for §10's staging tree.
 *
 * `report.ok !== true` and not `!report.ok`: `ok` arrives from JSON and an absent key must not
 * read as a green light. [M4.14 step E4, finding 2.3]
 */
export function importDisabled({ busy = false, phase = IDLE, report = null } = {}) {
  if (busy) return true;
  if (!report || report.ok !== true) return true;
  return phase !== VALIDATED;
}

/**
 * How many of §10's four steps — validate, report, swap, active — this phase has reached.
 *
 * A failure does not advance the strip. The steps are a claim about what the install did, and an
 * import that was refused, crashed or was lost never reached the swap; lighting it because the
 * operator pressed the button would make the strip a record of intentions. `UNKNOWN` stops at
 * the same place for the same reason: this page does not know that a swap happened, and the two
 * lit steps it keeps are the two it watched happen.
 *
 * `hasReport` is the second half of that honesty and costs one argument. §10's second step is
 * "report", and the refusals that never produce one — a path outside `DATA_DIR`, a file that is
 * not an archive, an archive that will not open, all 400s from `api/artifacts._resolve` and
 * `_open` — would otherwise light it beside an empty panel.
 */
export function stepsLit(phase, hasReport = true) {
  if (phase === IMPORTED) return 4;
  if (phase === RUNNING) return 3;
  if (phase === VALIDATED || phase === FAILED || phase === UNKNOWN) return hasReport ? 2 : 1;
  return 0;
}

/**
 * Which terminal phase a failed `POST /admin/bundle/import` leaves the screen in.
 *
 * The distinction is whether the SERVER answered. A 400, a 409 ("already running", "already
 * queued") and a 422 (the report says no) are answers: nothing was enqueued, the install is
 * exactly as it was, and `FAILED` with the report beside it is the whole truth. Anything else —
 * a 5xx, a proxy that cut the connection, a browser that gave up on a fetch `api.js` never
 * bounded — leaves a request that may have inserted a `job_run` row on its way out, and the
 * worker will act on that row whatever this tab believes. §2 puts the origin behind Cloudflare,
 * which cuts at 100 s, so this is not a hypothetical branch; it is the branch finding 2.1 was
 * measured in. [M4.14 step E4, findings 2.1 and 2.3]
 */
export function phaseForImportError(err) {
  const status = err?.status;
  return typeof status === 'number' && status >= 400 && status < 500 ? FAILED : UNKNOWN;
}

const wallClock = () => Date.now();
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Ask `/admin/bundle/state` until the import leaves the in-flight phases, or until this page's
 * own deadline. Spec v2.1 §5.3, §10; decision 253.
 *
 * TAKES ITS READER RATHER THAN IMPORTING ONE. `$lib/api.js` is the transport and this is the
 * rule; passing the read in is also the only way the deadline can be asserted at all, since
 * `api.js` deliberately sets no timeout and M4.14 does not touch it. The clock and the sleep are
 * injected for the same reason — a test that waited eleven real minutes to prove a deadline is a
 * test nobody runs.
 *
 * A READ THAT FAILS IS NOT AN OUTCOME. The import is running in the worker whether or not this
 * tab can reach the backend, and converting one lost GET into "it failed" is finding 2.1's own
 * defect one layer up — the operator told an import failed while it completed and flipped. So a
 * failed read is remembered and the poll goes on asking until the deadline, which is where this
 * page stops guessing and says `UNKNOWN_OUTCOME`.
 *
 * The first read happens before the first sleep, so an import that was already terminal when the
 * 202 arrived (a fixture bundle the worker claimed immediately) costs one request and no timer.
 *
 * A POLL OUTLIVES ITS SCREEN UNLESS SOMEONE ENDS IT, and `abandoned` is how the caller says its
 * screen is gone. Svelte destroying a component does not stop an async function that component
 * started, so an operator who left the Data tab mid-import left a poll reading `/admin/bundle/
 * state` every two seconds for the rest of the eleven minutes — against a backend whose pool is
 * busy with the import — and returning to the tab added a second poll rather than replacing the
 * first. This returns `null` for a poll nobody is waiting on any more: the ABSENCE of an
 * outcome, not a seventh phase for a destroyed screen to render.
 * [M4.14 cycle 4, m414-c4-dim202-02]
 */
export async function pollImportJob(readState, jobId, options = {}) {
  const {
    intervalMs = POLL_INTERVAL_MS,
    deadlineMs = POLL_DEADLINE_MS,
    now = wallClock,
    sleep = wait,
    abandoned = () => false
  } = options;
  const started = now();
  // The row this poll is ENTITLED to speak for, kept apart from whatever `/state` last returned.
  // The `job_id` test below gated only the early return, so a poll that ran to its deadline while
  // a second operator's row was the newest handed THAT row back -- and the one caller renders
  // `outcome.job.report` as this screen's verdict with no id test of its own, which is somebody
  // else's migration report printed under this page's "outcome unknown" banner. The filter and
  // the thing returned have to be the same filter. [M4.14 review cycle 1, waveE-03]
  let mine = null;
  let error = '';
  for (;;) {
    if (abandoned()) return null;
    try {
      const state = await readState();
      if (abandoned()) return null;
      error = '';
      const job = state?.import_job ?? null;
      // `job_id` and not "the newest row": a second operator's import would otherwise end this
      // one's poll with somebody else's verdict on this screen's report. Unconditionally, with
      // no id-is-absent disjunct beside it: the only caller has always had an id -- off the 202
      // or off the row it adopted -- so the off-switch was reachable by nobody and tested by
      // nothing, and the one future caller it invited is the lost-202 recovery ("poll without an
      // id and adopt whatever is running"), which is the outcome this guard exists to stop.
      // A recovery that wants the newest running row should find it and then poll it BY id.
      // [M4.14 cycle 4, m414-c4-dim202-06]
      if (job && job.job_id === jobId) {
        mine = job;
        const phase = phaseOfJob(job);
        if (phase !== RUNNING) return { phase, job, error };
      }
    } catch (err) {
      error = err?.message ?? String(err);
    }
    if (now() - started >= deadlineMs) return { phase: UNKNOWN, job: mine, error };
    await sleep(intervalMs);
  }
}

// One `detail` value's worth of text, and the same excerpt rule the report's own `render()`
// applies (`importer/report._DETAIL_ITEMS` / `_DETAIL_CHARS`).
const DETAIL_ITEMS = 5;
const DETAIL_CHARS = 96;

/**
 * One line of a failing finding's `detail`, for the Data tab. M4.14 finding 2.22.
 *
 * The detail dict is a failure's remediation information — the denied tables, the columns a
 * mapping names and the bundle lacks, the first offending title ids — and the page rendered the
 * message alone, so rule 7's "3 denied table(s)" reached the one person who has to go and fix
 * them with no table named. `render()` gained these lines in the same milestone; this is the
 * same excerpt so that the text an operator pastes into a ticket and the page they read it on
 * cannot describe one import differently. The count always survives the truncation, which is
 * what keeps an excerpt a report rather than a sample.
 */
export function detailLine(value) {
  if (Array.isArray(value)) {
    const head = value.slice(0, DETAIL_ITEMS).map((v) => `${v}`).join(', ');
    const tail = value.length > DETAIL_ITEMS ? `, ... (${value.length} total)` : '';
    return `${clip(head)}${tail}`;
  }
  return clip(`${value}`);
}

function clip(text) {
  return text.length > DETAIL_CHARS ? `${text.slice(0, DETAIL_CHARS - 3)}...` : text;
}

/**
 * A finding's detail as `[key, line]` pairs, sorted, or an empty list for a finding that carries
 * none. Sorted for `render()`'s reason: this text is what two imports get compared by.
 */
export function detailPairs(finding) {
  const detail = finding?.detail ?? {};
  return Object.keys(detail)
    .sort()
    .map((key) => [key, detailLine(detail[key])]);
}
