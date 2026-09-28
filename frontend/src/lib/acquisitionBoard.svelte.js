// Pure rules over the server's envelope. The stage names and each job's admitted actions come
// from the server; the plain step names below only sit beside them.

/** Spelled as the server spells them in `actions`. */
export const RETRY = 'retry';
export const RETRY_FROM = 'retry_from';
export const ABANDON = 'abandon';

export const DONE = 'done';
export const CURRENT = 'current';
export const UNREACHED = 'unreached';

// Keyed by the server's stage name, which stays on screen verbatim under each.
const STEP = {
  identify: 'Match the title',
  enrich: 'Gather details',
  derive: 'Work out features',
  'reviews gate': 'Check for reviews',
  'dna pack': 'Collect review snippets',
  'dna extract': "Read what it's like",
  verify: 'Double-check',
  project: 'Map to taste',
  place: 'Place on shelves',
  ready: 'Ready'
};

// The steps whose parks wait on film details arriving rather than on anything else.
const DETAIL_STEPS = new Set(['identify', 'enrich']);

/** @param {{name: string}} stage */
export function stepName(stage) {
  return STEP[stage?.name] ?? stage?.name ?? '';
}

// Parked is not broken (decision 336): the job waits on something that may change.
const STATUS = {
  parked: { tone: 'waiting', label: 'Waiting', badge: 'warn' },
  failed: { tone: 'broken', label: 'Failed', badge: 'bad' },
  abandoned: { tone: 'stopped', label: 'Stopped', badge: '' },
  queued: { tone: 'moving', label: 'Queued', badge: '' },
  running: { tone: 'moving', label: 'Running', badge: '' },
  ready: { tone: 'finished', label: 'Ready', badge: 'ok' }
};

/**
 * An unknown status is shown as the server spelled it.
 *
 * @param {{status: string, stage?: number}} job
 * @param {{number: number, name: string}[]} [stages]
 * @returns {{tone: string, label: string, badge: string}}
 */
export function statusOf(job, stages = []) {
  const known = STATUS[job?.status];
  if (!known) return { tone: 'other', label: String(job?.status), badge: '' };
  const at = stages.find((stage) => stage.number === Number(job.stage));
  if (job.status === 'parked' && DETAIL_STEPS.has(at?.name)) {
    return { ...known, label: 'Waiting for details' };
  }
  return known;
}

/**
 * A `ready` job has run its last stage, so that stage is done rather than current.
 *
 * @param {{number: number, name: string}[]} stages the envelope's legend, untouched
 * @param {{stage: number, status: string}} job
 */
export function segments(stages, job) {
  const reached = Number(job.stage);
  return stages.map((stage) => {
    let state = UNREACHED;
    if (stage.number < reached) state = DONE;
    else if (stage.number === reached) state = job.status === 'ready' ? DONE : CURRENT;
    return { number: stage.number, name: stage.name, plain: stepName(stage), state };
  });
}

export function progressOf(stages, job) {
  return {
    done: segments(stages, job).filter((seg) => seg.state === DONE).length,
    total: stages.length
  };
}

const KIND = { movie: 'Film', series: 'Series' };

export function metaOf(job) {
  return [KIND[job?.title_kind], job?.year].filter(Boolean).join(' · ');
}

/**
 * One plain sentence from the job's own facts; the verbatim reason stays under Technical details.
 *
 * @param {{number: number, name: string}[]} stages
 * @param {{stage: number, status: string}} job
 */
export function summaryOf(stages, job) {
  const at = stages.find((stage) => stage.number === Number(job.stage));
  const where = at
    ? `step ${at.number} of ${stages.length} (${stepName(at)})`
    : `step ${job.stage}`;
  switch (job.status) {
    case 'parked':
      return `Waiting at ${where}. Nothing is broken: it waits for something that may change.`;
    case 'failed':
      return (
        `Something went wrong at ${where}, and it will again until the cause is fixed. ` +
        'Technical details says what happened.'
      );
    case 'abandoned':
      return `Stopped at ${where}. Nothing runs for this title until it's retried from a step.`;
    case 'queued':
      return `Due to run from ${where}.`;
    case 'running':
      return `Working on ${where} right now.`;
    case 'ready':
      return "Every step has run, so it's on your shelves.";
    default:
      return `At ${where}.`;
  }
}

/** The server's board stops at this many titles, most recently moved first. */
export const BOARD_LIMIT = 200;

export const WAITING = 'waiting';
export const READY = 'ready';
export const ALL = 'all';

// Waiting first: parked and failed are what the admin opens this for.
export const FILTERS = [
  { key: WAITING, label: 'Waiting', tones: ['broken', 'waiting'] },
  { key: READY, label: 'Ready', tones: ['finished'] },
  { key: ALL, label: 'All', tones: null }
];

const GROUPS = [
  { tone: 'broken', heading: 'Failed' },
  { tone: 'waiting', heading: 'Waiting' },
  { tone: 'moving', heading: 'In progress' },
  { tone: 'stopped', heading: 'Stopped' },
  { tone: 'finished', heading: 'Ready' },
  { tone: 'other', heading: 'Other' }
];

function shown(filter, job) {
  const tones = FILTERS.find((f) => f.key === filter)?.tones;
  return !tones || tones.includes(statusOf(job).tone);
}

/** @returns {Record<string, number>} each filter's count, keyed by filter */
export function filterCounts(jobs) {
  return Object.fromEntries(
    FILTERS.map((f) => [f.key, (jobs ?? []).filter((job) => shown(f.key, job)).length])
  );
}

/** The filter's jobs in status groups, server order kept inside each; empty groups left out. */
export function groupsOf(jobs, filter) {
  const kept = (jobs ?? []).filter((job) => shown(filter, job));
  return GROUPS.map((group) => ({
    ...group,
    jobs: kept.filter((job) => statusOf(job).tone === group.tone)
  })).filter((group) => group.jobs.length > 0);
}

/** @param {{actions?: string[], status?: string}} job */
export function offered(job) {
  const actions = Array.isArray(job.actions) ? job.actions : [];
  return {
    retry: actions.includes(RETRY),
    retryFrom: actions.includes(RETRY_FROM),
    abandon: actions.includes(ABANDON)
  };
}

/** A retry never moves a job forward (decision 444). */
export function retryStages(stages, job) {
  const reached = Number(job.stage);
  return stages.filter((stage) => stage.number >= 1 && stage.number <= reached);
}

/**
 * @param {string} action one of RETRY, RETRY_FROM, ABANDON
 * @param {number} titleId
 * @param {number | string} [stage] the stage a RETRY_FROM resumes at
 */
export function actionRequest(action, titleId, stage) {
  const base = `/admin/acquisition/${titleId}`;
  if (action === RETRY) return { path: `${base}/retry`, body: undefined };
  if (action === RETRY_FROM) return { path: `${base}/retry-from`, body: { stage: Number(stage) } };
  if (action === ABANDON) return { path: `${base}/abandon`, body: undefined };
  throw new Error(`the board has no action named ${action}`);
}

/** `api.js` passes the route's refusal sentence through as the message, so add nothing. */
export function refusalOf(err) {
  return err?.message || 'The server refused this and did not say why.';
}

/** Metadata only: the bytes live under /data/raw, which only the worker mounts, so `url` is text. */
export function documentFacts(doc) {
  return [
    ['source', doc.source ?? ''],
    ['kind', doc.kind ?? ''],
    ['url', doc.url ?? ''],
    ['http', doc.http_status == null ? '' : String(doc.http_status)],
    ['sha256', doc.content_sha256 ?? ''],
    ['bytes', doc.byte_size == null ? '' : String(doc.byte_size)],
    ['fetched', doc.fetched_at ?? ''],
    ['error', doc.error ?? '']
  ].filter(([, value]) => value !== '');
}
