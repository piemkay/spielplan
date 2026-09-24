/**
 * §6.6 Data's acquisition board: what a job's segments, status and actions are, read off the
 * server's envelope. Spec v2.1 §6.6 Data, §8; decisions 336, 345, 424, 444.
 *
 * PURE FUNCTIONS, FOR `bundleImport.svelte.js`'S REASON. The component keeps its runes; this
 * module keeps the rules, which are the half worth asserting without a browser.
 *
 * NOTHING HERE KNOWS §8'S TEN NAMES. `GET /api/admin/acquisition` carries `stages` out of the
 * driver's one tuple (`acquire/actions.stage_legend`, over `pipeline.STAGES`), and the board draws
 * its segments from that and from nothing else - plan A2's "one tuple, shared with the backend, so
 * the two spellings cannot drift". A list typed here would be a second spelling that nothing
 * compares with the first; `test_data_surface_guards.py` refuses one anywhere under frontend/src.
 *
 * NOR WHICH ACTIONS A STATE ADMITS. Each job carries `actions`, which is decision 444's table as
 * `acquire/actions.admitted` reads it, so the board offers exactly what the server will accept and a
 * plain retry cannot reappear on a parked job (decision 336) because a client guessed.
 */

/** Decision 444's three actions, spelled as the server spells them in `actions`. */
export const RETRY = 'retry';
export const RETRY_FROM = 'retry_from';
export const ABANDON = 'abandon';

/** A segment's three states: behind the job, where it stands, not yet reached. */
export const DONE = 'done';
export const CURRENT = 'current';
export const UNREACHED = 'unreached';

/**
 * How each board status reads, in words and as a tone the stylesheet colours.
 *
 * Decision 336 is the reason this is a table and not the raw status: `parked` already means
 * something non-failing in the shipped tree - `placement/reconcile._park_thin` parks
 * thin-but-placed titles at stage 2, and the title is ready while the job waits - so parked says
 * "waiting on something that may change" and failed says a stage raised. Abandoned (0029's fourth
 * outcome) is neither, and says what brings it back.
 */
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
 * The status as the board shows it. A status this build has no words for is shown as the server
 * spelled it, rather than guessed at: the board's job is to report the pipeline, not to tidy it.
 *
 * @param {string} status
 * @returns {{tone: string, label: string}}
 */
export function statusOf(status) {
  return STATUS[status] ?? { tone: 'other', label: String(status) };
}

/**
 * One job's segments, one per stage of the legend, in the legend's order.
 *
 * `stage` is the stage the board row stands at. A `ready` job stands at the last stage having run
 * it, so its own stage is done rather than current; every other status is still at that stage.
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

/**
 * The stage a job stands at, named by the legend: "2 enrich". Empty when the legend has no such
 * number, which would be a server that disagrees with itself and is not this module's to paper over.
 */
export function stageLabel(stages, job) {
  const found = stages.find((stage) => stage.number === Number(job.stage));
  return found ? `${found.number} ${found.name}` : String(job.stage);
}

/**
 * Which of the three controls the job offers, read off `job.actions` and nothing else.
 *
 * @param {{actions?: string[], status?: string}} job
 */
export function offered(job) {
  const actions = Array.isArray(job.actions) ? job.actions : [];
  return {
    retry: actions.includes(RETRY),
    retryFrom: actions.includes(RETRY_FROM),
    abandon: actions.includes(ABANDON)
  };
}

/**
 * The stages a retry may resume at: 1 to the stage the job reached, named by the legend.
 * Decision 444 - a retry never moves a job forward; the server refuses one past `stage` anyway.
 */
export function retryStages(stages, job) {
  const reached = Number(job.stage);
  return stages.filter((stage) => stage.number >= 1 && stage.number <= reached);
}

/**
 * The request one action makes: the path under `/api` and the body, if it takes one.
 *
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

/**
 * What the board shows when an action is refused: the server's sentence, whole.
 *
 * `api.js` puts a string `detail` into the error's message untouched, and `api/acquisition.py`
 * makes the refusal's own sentence that detail - the meter's "over spend cap" reason for a paid
 * retry, decision 336's for a plain retry of a parked job - so the board adds nothing to it.
 */
export function refusalOf(err) {
  return err?.message || 'the server refused this and did not say why';
}

/**
 * One `raw_document` row as the board lists it: METADATA, never the document (decision 345).
 *
 * `url` is returned as text for the component to print, never to link: the bytes live under
 * `/data/raw`, which only the worker mounts (M4.7 sec-08), and a link would invite a click the
 * backend cannot serve and must not.
 */
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
