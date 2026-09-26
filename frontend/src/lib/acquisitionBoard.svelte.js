// Pure rules over the server's envelope. The stage names and each job's admitted actions come
// from the server; a second spelling here would drift.

/** Spelled as the server spells them in `actions`. */
export const RETRY = 'retry';
export const RETRY_FROM = 'retry_from';
export const ABANDON = 'abandon';

export const DONE = 'done';
export const CURRENT = 'current';
export const UNREACHED = 'unreached';

// Parked is not broken (decision 336): the title is ready and the job waits on something.
const STATUS = {
  parked: { tone: 'waiting', label: 'parked - waiting on something that may change' },
  failed: { tone: 'broken', label: 'failed - a stage raised and will raise again' },
  abandoned: {
    tone: 'stopped',
    label: 'abandoned - nothing runs until it is retried from a stage'
  },
  queued: { tone: 'moving', label: 'queued - due to be walked' },
  running: { tone: 'moving', label: 'running - a worker is walking it now' },
  ready: { tone: 'finished', label: 'ready - every stage has run' }
};

/**
 * An unknown status is shown as the server spelled it.
 *
 * @param {string} status
 * @returns {{tone: string, label: string}}
 */
export function statusOf(status) {
  return STATUS[status] ?? { tone: 'other', label: String(status) };
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
    return { number: stage.number, name: stage.name, state };
  });
}

export function stageLabel(stages, job) {
  const found = stages.find((stage) => stage.number === Number(job.stage));
  return found ? `${found.number} ${found.name}` : String(job.stage);
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
  return err?.message || 'the server refused this and did not say why';
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
