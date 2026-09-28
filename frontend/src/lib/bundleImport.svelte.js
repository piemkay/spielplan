// Pure rules, not module `$state`: the wizard and Movie data mount the same importer, and
// module state would share one screen's phase with the other.

export const IDLE = 'idle';
/** A validation report is on screen; only an `ok` one arms the import. */
export const VALIDATED = 'validated';
/** The 202 landed and a `job_run` row is queued or running in the worker. */
export const RUNNING = 'running';
/** The worker finished and flipped the row. See `servedAfterImport` for whether it is loaded. */
export const IMPORTED = 'imported';
/** The import was refused, or it ran and failed. A stored report says which. */
export const FAILED = 'failed';
/** Nothing on this page can say what the install is doing. The honest sixth state. */
export const UNKNOWN = 'unknown';

/** Names the next action; ASCII, since a failing vitest prints it. */
export const UNKNOWN_OUTCOME =
  'We cannot tell whether the import started. Check what Movie data shows before you try again.';

// Fast enough to catch the terminal phase, cheap against a ~127s import.
export const POLL_INTERVAL_MS = 2_000;

// Twice the worker's 300s attempt ceiling plus the claim: past it the row is terminal either way.
export const POLL_DEADLINE_MS = 660_000;

// Per read: the deadline is only tested between reads, and `api.js` sets no timeout.
export const POLL_READ_TIMEOUT_MS = POLL_INTERVAL_MS * 5;

// `queued` and `running` are one phase here: "may I press anything?" has one answer for both.
const PHASE_OF_JOB = {
  queued: RUNNING,
  running: RUNNING,
  active: IMPORTED,
  failed: FAILED
};

// `null` means no import to report; an unrecognised phase is UNKNOWN, never a finished import.
export function phaseOfJob(job) {
  if (!job) return null;
  return PHASE_OF_JOB[job.phase] ?? UNKNOWN;
}

// Stated as what it permits: only an `ok` report while VALIDATED, never after a failure or
// during a run. `ok !== true`, because JSON may omit the key.
export function importDisabled({ busy = false, phase = IDLE, report = null } = {}) {
  if (busy) return true;
  if (!report || report.ok !== true) return true;
  return phase !== VALIDATED;
}

// A failure does not advance the strip, and a refusal with no report lights only one step.
export function stepsLit(phase, hasReport = true) {
  if (phase === IMPORTED) return 4;
  if (phase === RUNNING) return 3;
  if (phase === VALIDATED || phase === FAILED || phase === UNKNOWN) return hasReport ? 2 : 1;
  return 0;
}

// A 4xx is an answer and nothing was enqueued; anything else may have left a queued job behind
// (the proxy cuts at 100s), so the outcome is UNKNOWN.
export function phaseForImportError(err) {
  const status = err?.status;
  return typeof status === 'number' && status >= 400 && status < 500 ? FAILED : UNKNOWN;
}

const wallClock = () => Date.now();
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// The reader, clock and sleep are injected so the deadline is testable. A failed read is not an
// outcome: keep asking until the deadline. `abandoned` ends a poll whose screen is gone (null).
export async function pollImportJob(readState, jobId, options = {}) {
  const {
    intervalMs = POLL_INTERVAL_MS,
    deadlineMs = POLL_DEADLINE_MS,
    now = wallClock,
    sleep = wait,
    abandoned = () => false
  } = options;
  const started = now();
  // Only this job's row may be returned, never another operator's newest.
  let mine = null;
  let error = '';
  for (;;) {
    if (abandoned()) return null;
    try {
      const state = await readState();
      if (abandoned()) return null;
      error = '';
      const job = state?.import_job ?? null;
      if (job && job.job_id === jobId) {
        mine = job;
        const phase = phaseOfJob(job);
        // The read that ends the poll is the one the backend re-pinned on, so it says whether it is served.
        if (phase !== RUNNING) return { phase, job, error, state };
      }
    } catch (err) {
      error = err?.message ?? String(err);
    }
    if (now() - started >= deadlineMs) return { phase: UNKNOWN, job: mine, error };
    await sleep(intervalMs);
  }
}

/** The bundle the import flipped is loaded and serving: nothing is owed. */
export const LIVE = 'live';
/** The flip is real and the backend could not load it by itself: §10's restart is owed. */
export const RESTART = 'restart';

// The backend loads the flipped bundle on the read that reports it (decision 497), so a restart
// is owed only when the server says so; null when the payload cannot say.
export function servedAfterImport(state) {
  if (!state) return null;
  if (state.restart_required === true) return RESTART;
  if (state.active && state.loaded?.version === state.active) return LIVE;
  return null;
}

// The same excerpt rule as the report's own `render()` (`importer/report._DETAIL_*`).
const DETAIL_ITEMS = 5;
const DETAIL_CHARS = 96;

// The same excerpt as `render()`, so a ticket and the page describe one import alike.
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

// Sorted, as `render()` does: this text is what two imports get compared by.
export function detailPairs(finding) {
  const detail = finding?.detail ?? {};
  return Object.keys(detail)
    .sort()
    .map((key) => [key, detailLine(detail[key])]);
}
