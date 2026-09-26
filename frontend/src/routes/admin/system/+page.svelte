<script>
  // Read-only: the log level filter is the one control, and it asks the server nothing. The key
  // itself never appears, only its truncated fingerprint (§14.3).
  import { onMount } from 'svelte';
  import { get } from '$lib/api.js';
  import AdminTabs from '$lib/components/AdminTabs.svelte';

  // `card`, not `state`: `let state = $state(...)` trips a svelte-check error.
  let card = $state(null);
  let error = $state('');
  let level = $state('all');

  onMount(async () => {
    try {
      card = await get('/admin/system');
    } catch (err) {
      error = err.message || String(err);
    }
  });

  const stamp = (iso) => (iso ? new Date(iso).toLocaleString() : null);

  // The age is the fact: §2 promises one dump a night.
  function ago(iso) {
    if (!iso) return '';
    const minutes = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
    if (minutes < 60) return `${minutes} minute${minutes === 1 ? '' : 's'} ago`;
    const hours = Math.round(minutes / 60);
    if (hours < 48) return `${hours} hour${hours === 1 ? '' : 's'} ago`;
    const days = Math.round(hours / 24);
    return `${days} days ago`;
  }

  const megabytes = (bytes) =>
    typeof bytes === 'number' ? `${(bytes / 1_000_000).toFixed(1)} MB` : null;

  // `finished_at === null` means started and never reported (a kill, an OOM), not unknown.
  const outcome = (job) => (job.finished_at === null ? 'unfinished' : job.ok ? 'ok' : 'failed');

  // Scalar entries only, so a nested field can never render as [object Object].
  function detail(job) {
    const d = job.detail;
    if (!d || typeof d !== 'object') return '';
    if (typeof d.error === 'string') return d.error;
    return Object.entries(d)
      .filter(([, v]) => v === null || ['string', 'number', 'boolean'].includes(typeof v))
      .slice(0, 4)
      .map(([k, v]) => `${k} ${v}`)
      .join(' · ');
  }

  // The route always sends all five, zeros included.
  const STATES = ['pending', 'leased', 'done', 'failed', 'skipped'];

  /** `by_kind` regrouped per kind, for "identify: pending 3 · failed 1" -- an operator's sentence. */
  function kinds(rows) {
    const grouped = new Map();
    for (const row of rows ?? []) {
      if (!grouped.has(row.kind)) grouped.set(row.kind, []);
      grouped.get(row.kind).push(`${row.state} ${row.count}`);
    }
    return [...grouped].map(([kind, parts]) => ({ kind, line: parts.join(' · ') }));
  }

  // The worker's storage probe, lifted out of the jobs list: it explains the failures below it.
  // The exception class name `_tick` prefixes is stripped from its sentence.
  const STORAGE_JOB = 'storage-check';
  const storageRow = $derived((card?.jobs ?? []).find((job) => job.name === STORAGE_JOB) ?? null);
  const storageFailure = (job) =>
    typeof job?.detail?.error === 'string'
      ? job.detail.error.replace(/^[A-Za-z]+Error: /, '')
      : 'the check failed without saying why - read the worker log';

  // Python's level names as `core/logs` records them; CRITICAL is above ERROR.
  const RANKS = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 };
  const FLOORS = { all: 0, warning: 30, error: 40 };

  // Newest first: the line an operator came for is the one that just happened.
  const lines = $derived(
    [...(card?.logs?.records ?? [])]
      .reverse()
      .filter((r) => (RANKS[r.level] ?? 0) >= FLOORS[level])
  );
</script>

<AdminTabs active="system" />

<h1>System</h1>
<p class="why">
  Read-only: whether last night's dump happened, which SECRETS_KEY this process holds, whether the
  app can write its data directories, how each background job last ended, how much acquisition
  work is waiting, when each connector last synced successfully, and what this process has logged
  since it started.
</p>

{#if error}
  <p class="err" role="alert">{error}</p>
{:else if !card}
  <p class="data">loading…</p>
{:else}
  <!-- The newest successful dump, not the newest attempt. -->
  <section class="card fact" data-testid="system-backup">
    <h2>Last successful backup</h2>
    {#if card.backup.at}
      <div class="data-lg">
        {stamp(card.backup.at)} · {ago(card.backup.at)}{megabytes(card.backup.bytes)
          ? ` · ${megabytes(card.backup.bytes)}`
          : ''}
      </div>
    {:else}
      <div class="data-lg">no dump has ever completed on this install</div>
    {/if}
    {#if card.backup.stale}
      <p class="warn" data-testid="system-backup-stale">
        No successful dump in the last {card.backup.stale_after_hours} hours. Check the
        nightly-backup row below, then <code class="data-lg">ls -l data/backups</code> — a
        <code class="data-lg">.partial</code> file is not a backup.
      </p>
    {/if}
  </section>

  <!-- Which key is loaded, never what it is: the stored credential is admin-equivalent (§14.3). -->
  <section class="card fact" data-testid="system-secrets">
    <h2>Secrets custody</h2>
    {#if card.secrets.configured}
      <div class="data-lg">
        SECRETS_KEY fingerprint <code>{card.secrets.fingerprint}</code> · active key_id
        <code>{card.secrets.key_id ?? 'none yet'}</code>
      </div>
    {:else}
      <div class="data-lg">SECRETS_KEY is not set</div>
      <p class="why">
        A legal state (§3.1): the app runs, and every connector that would store a credential
        refuses until the key is set.
      </p>
    {/if}
    {#if card.secrets.unreadable}
      <!-- "every stored secret": after a key reset the active key reads while older sealed rows do not. -->
      <p class="warn" data-testid="system-secrets-unreadable">
        SECRETS_KEY does not open every stored secret: at least one is sealed under a key this
        install no longer holds. Connector credentials stay sealed and member writes still
        commit — restore the <code class="data-lg">.env</code> that was current when the dump was
        taken, or run <code class="data-lg">spielplan-secrets reset</code> and re-enter the API
        key on Connectors.
      </p>
    {/if}
  </section>

  <section class="card fact" data-testid="system-storage">
    <h2>Storage</h2>
    {#if !storageRow}
      <div class="data-lg" data-storage="unchecked">
        not checked yet - the worker checks its five data directories on its first run
      </div>
    {:else if outcome(storageRow) === 'ok'}
      <div class="data-lg" data-storage="ok">
        all five data directories writable · checked {ago(storageRow.finished_at)}
      </div>
    {:else if outcome(storageRow) === 'failed'}
      <p class="warn" data-storage="unwritable">{storageFailure(storageRow)}</p>
    {:else}
      <div class="data-lg" data-storage="unfinished">
        the last check started {ago(storageRow.started_at)} and never reported
      </div>
    {/if}
  </section>

  <section class="fact" data-testid="system-jobs">
    <h2>Jobs</h2>
    {#if card.jobs.length === 0}
      <p class="why" data-empty="jobs">
        The worker has not recorded a run yet. On a stack that has just started this is
        expected — the first tick is seconds away, and the first nightly is tonight.
      </p>
    {:else}
      <ul class="jobs">
        {#each card.jobs as job (job.name)}
          <li data-testid="system-job" data-job={job.name} data-outcome={outcome(job)}>
            <span class="name">{job.name}</span>
            <span class="data facts">
              {outcome(job)} · {ago(job.finished_at ?? job.started_at)}
            </span>
            {#if detail(job)}<span class="data note">{detail(job)}</span>{/if}
          </li>
        {/each}
      </ul>
      <p class="why">
        The row is opened before the job runs, so “unfinished” means it started and never
        reported — a kill, an OOM or a power cut — rather than that nothing is known.
      </p>
    {/if}
  </section>

  {#if card.queue}
    <section class="fact" data-testid="system-queue">
      <h2>Acquisition queue</h2>
      <div class="data-lg">
        {STATES.map((s) => `${s} ${card.queue.by_state?.[s] ?? 0}`).join(' · ')}
      </div>
      {#if kinds(card.queue.by_kind).length}
        <ul class="plain">
          {#each kinds(card.queue.by_kind) as row (row.kind)}
            <li class="data" data-queue-kind={row.kind}>{row.kind}: {row.line}</li>
          {/each}
        </ul>
      {:else}
        <p class="why" data-empty="queue">Nothing has been filed for acquisition yet.</p>
      {/if}
    </section>
  {/if}

  <!-- The newest successful run, which the jobs list's newest attempt cannot say. -->
  {#if card.last_syncs}
    <section class="fact" data-testid="system-last_syncs">
      <h2>Last successful syncs</h2>
      <ul class="jobs">
        {#each card.last_syncs as sync (sync.name)}
          <li data-last-sync={sync.name} data-synced={sync.at ? 'yes' : 'never'}>
            <span class="name">{sync.connector} · {sync.name}</span>
            <span class="data facts">
              {sync.at ? `${stamp(sync.at)} · ${ago(sync.at)}` : 'never succeeded'}
            </span>
          </li>
        {/each}
      </ul>
    </section>
  {/if}

  {#if card.logs}
    <section class="fact" data-testid="system-logs">
      <h2>Recent log lines</h2>
      <label class="level">
        <span class="data">LOG LEVEL</span>
        <select bind:value={level}>
          <option value="all">all (info and above)</option>
          <option value="warning">warnings and errors</option>
          <option value="error">errors only</option>
        </select>
      </label>
      <p class="why">
        This {card.logs.scope ?? 'web process'}'s own lines since {stamp(card.logs.since) ??
          'it started'}, the last 200 at info and above, emptied by a restart. The worker's lines
        are in its container log, and its failures are in the jobs list above.
      </p>
      {#if lines.length === 0}
        <p class="why" data-empty="logs">No line at this level since the process started.</p>
      {:else}
        <ol class="logs">
          {#each lines as line, i (i)}
            <li data-log-level={line.level}>
              <span class="data">{stamp(line.at)} · {line.level} · {line.logger}</span>
              <span class="data-lg message">{line.message}</span>
            </li>
          {/each}
        </ol>
      {/if}
    </section>
  {/if}
{/if}

<style>
  h1 {
    margin: 0 0 8px;
    font-size: 19px;
    font-weight: 600;
  }
  h2 {
    margin: 0 0 8px;
    font-size: 14px;
    font-weight: 600;
  }
  .fact {
    margin: 14px 0;
  }
  .warn {
    margin: 10px 0 0;
    padding: 10px 13px;
    border: 1px solid var(--ember-edge);
    background: var(--ember-wash);
    border-radius: var(--r-md);
    font-size: 13px;
    /* The storage sentence carries paths and a command with no break opportunity in them. */
    overflow-wrap: anywhere;
  }
  .jobs {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .jobs li {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 4px 12px;
    padding: 11px 13px;
    border: 1px solid var(--line);
    border-radius: var(--r-sm);
  }
  /* A failed row is the one the page exists for, so it is the one that is not grey. */
  .jobs li[data-outcome='failed'] .facts,
  .jobs li[data-outcome='unfinished'] .facts {
    color: var(--ember-lift);
  }
  .name {
    font-size: 13.5px;
  }
  .note {
    flex-basis: 100%;
  }
  .plain {
    list-style: none;
    margin: 8px 0 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .level {
    display: flex;
    flex-direction: column;
    gap: 5px;
    max-width: 280px;
    margin-bottom: 8px;
  }
  .logs {
    list-style: none;
    margin: 8px 0 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .logs li {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 8px 10px;
    border: 1px solid var(--line);
    border-radius: var(--r-sm);
  }
  .logs li[data-log-level='ERROR'],
  .logs li[data-log-level='CRITICAL'] {
    border-color: var(--ember-edge);
  }
  .message {
    word-break: break-word;
  }
  .err {
    color: var(--ember-lift);
    font-size: 12.5px;
    margin: 8px 0;
  }
</style>
