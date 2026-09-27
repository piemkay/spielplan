// What Overview, the admin sidebar and System read (decision 527): one read per endpoint in flight,
// and plain sentences computed from the facts those reads already return.
import { get } from '$lib/api.js';

export const facts = $state({
  system: null,
  llm: null,
  sources: null,
  jellyfin: null,
  users: null,
  config: null,
  /** @type {Record<string, string>} */
  errors: {}
});

const READS = {
  system: '/admin/system',
  llm: '/admin/llm',
  sources: '/admin/connectors',
  jellyfin: '/admin/connectors/jellyfin',
  users: '/admin/users',
  config: '/config'
};

/** @type {Record<string, Promise<void>>} */
const inflight = {};

/** @param {string[]} [keys] */
export function refresh(keys = Object.keys(READS)) {
  return Promise.all(keys.map((key) => (inflight[key] ??= read(key))));
}

/** @param {string} key */
async function read(key) {
  try {
    facts[key] = await get(READS[key]);
    delete facts.errors[key];
  } catch (err) {
    facts.errors[key] = err.message || String(err);
  } finally {
    delete inflight[key];
  }
}

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

// A job that started this long ago and never reported was killed, not running.
const STALLED_AFTER = HOUR;

const JOB_LABELS = {
  'session-prune': 'Sign-in cleanup',
  'push-subscription-prune': 'Notification cleanup',
  'webauthn-challenge-prune': 'Passkey cleanup',
  'job-run-prune': 'Job history cleanup',
  'ledger-map-refit': 'Taste map refresh',
  'ledger-refresh': 'Taste refresh',
  'fold-in-user-vectors': 'Taste profiles',
  'fold-in-tick': 'Taste profile update',
  'tier-set-refit': 'Rank refresh',
  'placement-reconciliation': 'Shelf placement',
  'acquisition-drain': 'New titles',
  'metadata-backfill': 'Film info backfill',
  'art-lookup': 'Poster lookup',
  'jellyfin-seen-sync': 'Jellyfin sync',
  'jellyfin-sessions-poll': 'Playback watch',
  'jellyfin-delta-poll': 'New in Jellyfin',
  'jellyfin-intake-sweep': 'Library sweep',
  'bundle-import': 'Movie data import',
  'storage-check': 'Storage check',
  'nightly-backup': 'Nightly backup'
};

/** @param {string} name */
export const jobLabel = (name) => JOB_LABELS[name] ?? name;

/** `finished_at === null` means started and never reported (a kill, an OOM), not unknown. */
export const outcome = (job) => (job.finished_at === null ? 'unfinished' : job.ok ? 'ok' : 'failed');

/** Failed, or unfinished for longer than any job runs. */
export function troubled(job, now = Date.now()) {
  const state = outcome(job);
  if (state === 'failed') return true;
  return state === 'unfinished' && now - Date.parse(job.started_at) > STALLED_AFTER;
}

/** "just now", "4 min ago", "9 h ago", "3 days ago". */
export function ago(iso, now = Date.now()) {
  const ms = Math.max(0, now - Date.parse(iso));
  if (ms < MINUTE) return 'just now';
  if (ms < HOUR) return `${Math.round(ms / MINUTE)} min ago`;
  if (ms < 2 * DAY) return `${Math.round(ms / HOUR)} h ago`;
  return `${Math.round(ms / DAY)} days ago`;
}

/** "Today, 06:00", "Yesterday, 06:00", "26 Sep, 06:00". */
export function when(iso, now = Date.now()) {
  const at = new Date(iso);
  const time = at.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
  if (at.toDateString() === new Date(now).toDateString()) return `Today, ${time}`;
  if (at.toDateString() === new Date(now - DAY).toDateString()) return `Yesterday, ${time}`;
  return `${at.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })}, ${time}`;
}

export const money = (amount) => `$${Number(amount).toFixed(2)}`;
const count = (n, one, many = `${one}s`) => `${n.toLocaleString('en')} ${n === 1 ? one : many}`;
const decimal = (value) =>
  value !== null && value !== '' && Number.isFinite(Number(value)) ? Number(value) : null;

// `spend.ATTEMPTS`: a title reserves two attempts inside the cap before its first (decision 325).
const ATTEMPTS = 2;
// Micro-dollars, the gate's unit, so `<` here parks exactly what the gate parks.
const micro = (amount) => Math.round(Number(amount) * 1e6);

/** The month a spend period rolls into, read half a day past its end so no time zone misplaces it. */
const monthAfter = (periodEnd) =>
  new Date(Date.parse(periodEnd) + DAY / 2).toLocaleString('en-US', { month: 'long' });

function budgetCard(llm, now) {
  const meter = llm?.meter;
  if (!meter) return null;
  const card = { key: 'budget', icon: 'budget', href: '/admin/budget', action: 'Review budget' };
  if (meter.cap_usd === null) {
    return {
      ...card,
      headline: 'No AI budget is set',
      body: 'New titles wait for their details until you set a monthly limit.',
      action: 'Set a budget'
    };
  }
  const remaining = decimal(meter.remaining_usd);
  const until = monthAfter(meter.period_end);
  const perTitle = decimal(llm.estimate?.per_title_usd);
  const noRoom =
    remaining !== null && perTitle !== null && micro(remaining) < micro(perTitle) * ATTEMPTS;
  if (remaining !== null && (remaining <= 0 || noRoom)) {
    return {
      ...card,
      headline: 'The AI budget is used up',
      body: `New titles wait until ${until}, unless you raise the limit.`
    };
  }
  const monthly = decimal(llm.projected?.monthly_usd);
  const span = llm.projected?.window_days ?? 30;
  if (remaining === null || !monthly) return null;
  const daysLeft = remaining / (monthly / span);
  if (daysLeft >= (Date.parse(meter.period_end) - now) / DAY) return null;
  const days = Math.round(daysLeft);
  return {
    ...card,
    headline:
      days < 1
        ? 'The AI budget runs out today'
        : `The AI budget runs out in about ${count(days, 'day')}`,
    body:
      `At this pace new titles cost about ${money(monthly)} a month. ` +
      `Once ${money(meter.cap_usd)} is spent, they wait until ${until}.`
  };
}

/**
 * What needs the admin, most urgent first: each a headline, a sentence or two, and where it is fixed.
 * @returns {Array<{key: string, icon: string, headline: string, body: string, href: string, action: string}>}
 */
export function attention(f, now = Date.now()) {
  const items = [];
  const system = f.system;

  const sealed =
    system?.secrets?.unreadable ||
    f.jellyfin?.secrets_unreadable ||
    (f.sources?.sources ?? []).some((s) => s.secrets_unreadable);
  if (sealed) {
    items.push({
      key: 'secrets',
      icon: 'lock',
      headline: "Some saved keys can't be opened",
      body:
        "The encryption key doesn't match every saved key, so the services that use them have " +
        'stopped. System says how to fix it.',
      href: '/admin/system',
      action: 'Open System'
    });
  }

  const required = (f.sources?.sources ?? []).find(
    (s) => s.required && !s.has_api_key && !s.secrets_unreadable
  );
  if (required) {
    const name = SOURCE_NAMES[required.name] ?? required.name;
    items.push({
      key: 'required-key',
      icon: 'key',
      headline: `Film info needs a ${name} key`,
      body: `Without it new titles can't be matched, so they wait. Add the key in Services.`,
      href: '/admin/services',
      action: 'Add the key'
    });
  }

  const jobs = (system?.jobs ?? []).filter((job) => troubled(job, now));
  if (jobs.length) {
    const [job] = jobs;
    items.push({
      key: 'jobs',
      icon: 'pulse',
      headline:
        jobs.length > 1
          ? `${jobs.length} background jobs stopped working`
          : outcome(job) === 'failed'
            ? `${jobLabel(job.name)} failed`
            : `${jobLabel(job.name)} didn't finish`,
      body: 'System shows what went wrong and when each job last ran.',
      href: '/admin/system',
      action: 'Open System'
    });
  }

  if (system?.backup?.stale) {
    items.push({
      key: 'backup',
      icon: 'clock',
      headline: system.backup.at ? `The last backup was ${ago(system.backup.at, now)}` : 'No backup yet',
      body: system.backup.at
        ? 'Backups should run every night. System shows how the last one went.'
        : 'Nothing has been backed up on this install. System shows how the last attempt went.',
      href: '/admin/system',
      action: 'Open System'
    });
  }

  const budget = budgetCard(f.llm, now);
  if (budget) items.push(budget);

  const titles = system?.acquisition;
  if (titles?.failed) {
    items.push({
      key: 'titles-failed',
      icon: 'inbox',
      headline: `${count(titles.failed, 'new title')} couldn't be added`,
      body: 'Something went wrong while getting them ready. You can retry them from New titles.',
      href: '/admin/titles',
      action: 'See titles'
    });
  }
  if (titles?.parked) {
    const ready = titles.ready ? ` ${count(titles.ready, 'is', 'are')} ready.` : '';
    items.push({
      key: 'titles-waiting',
      icon: 'inbox',
      headline: `${count(titles.parked, 'new title is', 'new titles are')} waiting`,
      body: `Each one says what it's waiting for, and most carry on by themselves once it arrives.${ready}`,
      href: '/admin/titles',
      action: 'See titles'
    });
  }
  return items;
}

const SOURCE_NAMES = { tmdb: 'TMDB', omdb: 'OMDb', trakt: 'Trakt' };

/**
 * The four health checks: `state` is ok, warn, or wait (nothing to judge yet).
 * @returns {Array<{key: string, label: string, detail: string, state: string, href: string}>}
 */
export function health(f, now = Date.now()) {
  const system = f.system;
  const jobs = system?.jobs ?? [];
  const rows = [];

  const backup = system?.backup;
  rows.push({
    key: 'backups',
    label: 'Backups',
    detail: backup?.at ? when(backup.at, now) : 'Never',
    state: backup?.stale ? 'warn' : 'ok',
    href: '/admin/system'
  });

  const sync = (system?.last_syncs ?? []).find((s) => s.name === 'jellyfin-seen-sync');
  let jellyfin = { detail: 'Not set up', state: 'warn' };
  if (f.jellyfin?.configured) {
    if (!sync?.at) jellyfin = { detail: 'Not synced yet', state: 'warn' };
    else if (now - Date.parse(sync.at) > HOUR) {
      jellyfin = { detail: `Last synced ${ago(sync.at, now)}`, state: 'warn' };
    }
    else jellyfin = { detail: `Synced ${ago(sync.at, now)}`, state: 'ok' };
  }
  rows.push({ key: 'jellyfin', label: 'Jellyfin', ...jellyfin, href: '/admin/services' });

  const probe = jobs.find((job) => job.name === 'storage-check');
  const storage = !probe
    ? { detail: 'Not checked yet', state: 'wait' }
    : outcome(probe) === 'ok'
      ? { detail: 'All folders writable', state: 'ok' }
      : outcome(probe) === 'failed'
        ? { detail: "A folder isn't writable", state: 'warn' }
        : { detail: "The last check didn't finish", state: troubled(probe, now) ? 'warn' : 'wait' };
  rows.push({ key: 'storage', label: 'Storage', ...storage, href: '/admin/system' });

  const bad = jobs.filter((job) => troubled(job, now)).length;
  rows.push({
    key: 'jobs',
    label: 'Background jobs',
    detail: jobs.length ? `${jobs.length - bad} of ${jobs.length} healthy` : 'None have run yet',
    state: !jobs.length ? 'wait' : bad ? 'warn' : 'ok',
    href: '/admin/system'
  });
  return rows;
}

/** New titles waiting, for the sidebar's badge. */
export const waiting = (f) => f.system?.acquisition?.parked ?? 0;

/** The value each Manage row shows. */
export function manage(f, now = Date.now()) {
  const titles = f.system?.acquisition;
  const meter = f.llm?.meter;
  const bundle = f.config?.bundle;
  const fine = !attention(f, now).some((a) => ['secrets', 'jobs', 'backup'].includes(a.key));
  let titleValue = '';
  if (titles) {
    titleValue = titles.parked
      ? `${titles.parked.toLocaleString('en')} waiting`
      : titles.failed
        ? `${titles.failed} failed`
        : 'Up to date';
  }
  let budget = '';
  if (meter) {
    budget =
      meter.cap_usd === null ? 'No limit set' : `${money(meter.spent_usd)} of ${money(meter.cap_usd)}`;
  }
  let movieData = '';
  if (f.config) {
    movieData = bundle?.titles ? count(bundle.titles, 'title') : bundle ? bundle.version : 'Not imported';
  }
  return {
    titles: titleValue,
    people: f.users ? String(f.users.length) : '',
    services: 'Jellyfin, film info',
    budget,
    movieData,
    corrections: '3 lists',
    system: !f.system ? '' : fine ? 'Healthy' : 'Needs you'
  };
}
