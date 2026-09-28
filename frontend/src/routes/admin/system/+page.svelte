<script>
  // Read-only (§6.6): plain health on top, the verbatim facts one tap down. The log level is the one
  // control, and it asks the server nothing. The key itself never appears, only its fingerprint.
  import { onMount } from 'svelte';
  import Icon from '$lib/components/Icon.svelte';
  import { ago, facts, jobLabel, outcome, refresh, troubled, when } from '../overview.svelte.js';

  let level = $state('warning');

  onMount(() => {
    refresh(['system', 'config']);
  });

  const card = $derived(facts.system);
  const bundle = $derived(facts.config?.bundle ?? null);
  const now = $derived(card ? Date.now() : 0);

  const stamp = (iso) => (iso ? new Date(iso).toLocaleString('en-GB') : null);
  const megabytes = (bytes) => (typeof bytes === 'number' ? `${Math.round(bytes / 1_000_000)} MB` : '');

  // The worker's storage probe explains the failures around it; `_tick`'s exception class is dropped.
  const storageRow = $derived((card?.jobs ?? []).find((job) => job.name === 'storage-check') ?? null);
  const STORAGE = { ok: 'ok', failed: 'unwritable', unfinished: 'unfinished' };
  const storage = $derived(storageRow ? STORAGE[outcome(storageRow)] : 'unchecked');
  const storageFailure = (job) =>
    typeof job?.detail?.error === 'string'
      ? job.detail.error.replace(/^[A-Za-z]+Error: /, '')
      : 'The check failed without saying why. The worker log has the rest.';

  const jobs = $derived(card?.jobs ?? []);
  const unhealthy = $derived(jobs.filter((job) => troubled(job, now)).length);
  const finished = (job) => job.finished_at ?? job.started_at;
  // The three to look at first: trouble, then whatever ran last.
  const recent = $derived(
    [...jobs]
      .sort(
        (a, b) =>
          Number(troubled(b, now)) - Number(troubled(a, now)) ||
          Date.parse(finished(b)) - Date.parse(finished(a))
      )
      .slice(0, 3)
  );
  const said = (job) =>
    outcome(job) === 'ok'
      ? ago(finished(job), now)
      : `${outcome(job) === 'failed' ? 'Failed' : "Didn't finish"} · ${ago(finished(job), now)}`;

  const waitingWork = $derived((card?.queue?.by_state?.pending ?? 0) + (card?.queue?.by_state?.leased ?? 0));
  const STATES = ['pending', 'leased', 'done', 'failed', 'skipped'];

  /** `by_kind` regrouped per kind: "acquire: pending 3 · failed 1". */
  function kinds(rows) {
    const grouped = new Map();
    for (const row of rows ?? []) {
      if (!grouped.has(row.kind)) grouped.set(row.kind, []);
      grouped.get(row.kind).push(`${row.state} ${row.count}`);
    }
    return [...grouped].map(([kind, parts]) => ({ kind, line: parts.join(' · ') }));
  }

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

  // Python's level names as `core/logs` records them; CRITICAL is above ERROR.
  const RANKS = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 };
  const FLOORS = { all: 0, warning: 30, error: 40 };
  const LEVELS = { warning: 'Warnings and errors', error: 'Errors only', all: 'Everything' };

  // Newest first: the line an admin came for is the one that just happened.
  const lines = $derived(
    [...(card?.logs?.records ?? [])].reverse().filter((r) => (RANKS[r.level] ?? 0) >= FLOORS[level])
  );
</script>

{#snippet tile(state)}
  <Icon
    name={state === 'ok' ? 'check' : state === 'warn' ? 'warning' : 'clock'}
    tone={state === 'ok' ? 'green' : state}
  />
{/snippet}

{#snippet chevron()}
  <span class="chev"><Icon name="chevron-right" size={16} /></span>
{/snippet}

<div class="system">
  <h1 class="large-title">System</h1>

  {#if !card}
    {#if facts.errors.system}
      <p class="err" role="alert">{facts.errors.system}</p>
    {:else}
      <p class="footnote">Loading…</p>
    {/if}
  {:else}
    <section class="group">
      <h2 class="list-header">Health</h2>
      <div class="list-group lines">
        <!-- The newest successful dump, not the newest attempt. -->
        <div class="list-row" data-testid="system-backup">
          {@render tile(card.backup.stale ? 'warn' : 'ok')}
          <span class="text">
            <span>Backups</span>
            {#if megabytes(card.backup.bytes)}<span class="sub">{megabytes(card.backup.bytes)}</span>{/if}
          </span>
          <span class="value">{card.backup.at ? when(card.backup.at, now) : 'Never'}</span>
        </div>
        <div class="list-row" data-testid="system-secrets">
          {@render tile(!card.secrets.configured ? 'wait' : card.secrets.unreadable ? 'warn' : 'ok')}
          <span class="name">Encryption key</span>
          <span class="value">
            {!card.secrets.configured ? 'Not set' : card.secrets.unreadable ? "Can't open every key" : 'Loaded'}
          </span>
        </div>
        <div class="list-row" data-testid="system-storage" data-storage={storage}>
          {@render tile(storage === 'ok' ? 'ok' : storage === 'unwritable' ? 'warn' : 'wait')}
          <span class="name">Storage</span>
          <span class="value">
            {storage === 'ok'
              ? 'All writable'
              : storage === 'unwritable'
                ? "A folder isn't writable"
                : storage === 'unfinished'
                  ? "Last check didn't finish"
                  : 'Not checked yet'}
          </span>
        </div>
        <div class="list-row">
          {@render tile(!jobs.length ? 'wait' : unhealthy ? 'warn' : 'ok')}
          <span class="name">Background jobs</span>
          <span class="value">
            {jobs.length ? `${jobs.length - unhealthy} of ${jobs.length} healthy` : 'None run yet'}
          </span>
        </div>
        <div class="list-row" data-testid="system-bundle">
          {@render tile(bundle ? 'ok' : 'wait')}
          <span class="text">
            <span>Movie data</span>
            {#if bundle?.titles}<span class="sub">{bundle.titles.toLocaleString('en')} titles</span>{/if}
          </span>
          <span class="value">{bundle ? bundle.version : facts.config ? 'Not imported' : ''}</span>
        </div>
      </div>

      {#if card.backup.stale}
        <p class="alert" data-testid="system-backup-stale">
          <Icon name="warning" size={18} />
          <span>
            {card.backup.at
              ? `No backup has finished in the last ${card.backup.stale_after_hours} hours.`
              : 'No backup has finished on this install yet.'}
            The nightly backup under All jobs says how the last attempt went.
          </span>
        </p>
      {/if}
      {#if card.secrets.unreadable}
        <!-- "some": after a key reset the active key opens while older sealed rows do not. -->
        <p class="alert" data-testid="system-secrets-unreadable">
          <Icon name="warning" size={18} />
          <span>
            The encryption key can't open some saved keys, so the services that use them have
            stopped. Restore the <code class="code">.env</code> file from when the backup was made,
            or run <code class="code">spielplan-secrets reset</code> and enter the keys again in
            Services.
          </span>
        </p>
      {:else if !card.secrets.configured}
        <p class="list-footer">
          The app runs without an encryption key, but can't save a key for another service until
          one is set.
        </p>
      {/if}
      {#if storage === 'unwritable'}
        <div class="alert" data-storage="unwritable">
          <Icon name="warning" size={18} />
          <span>
            A data folder isn't writable, so saving can fail. To fix it:
            <span class="code block">{storageFailure(storageRow)}</span>
          </span>
        </div>
      {/if}
    </section>

    {#if card.queue}
      <section class="group" data-testid="system-queue">
        <h2 class="list-header">Work queue</h2>
        <div class="stats">
          <div class="stat"><span class="figure">{waitingWork.toLocaleString('en')}</span>Waiting</div>
          <div class="stat">
            <span class="figure">{(card.queue.by_state.done ?? 0).toLocaleString('en')}</span>Done
          </div>
          <div class="stat">
            <span class="figure">{(card.queue.by_state.skipped ?? 0).toLocaleString('en')}</span>Skipped
          </div>
          {#if card.queue.by_state.failed}
            <div class="stat bad">
              <span class="figure">{card.queue.by_state.failed.toLocaleString('en')}</span>Failed
            </div>
          {/if}
        </div>
      </section>
    {/if}

    <section class="group" data-testid="system-jobs">
      <h2 class="list-header">Recent jobs</h2>
      {#if jobs.length === 0}
        <p class="list-footer first" data-empty="jobs">
          The background worker hasn't run anything yet. Right after a start that's expected: its
          first run is moments away.
        </p>
      {:else}
        <div class="list-group lines">
          {#each recent as job (job.name)}
            <div class="list-row">
              <span class="dot {troubled(job, now) ? 'bad' : outcome(job) === 'ok' ? 'ok' : 'wait'}"></span>
              <span class="name">{jobLabel(job.name)}</span>
              <span class="value" class:badword={troubled(job, now)}>{said(job)}</span>
            </div>
          {/each}
          <details>
            <summary class="list-row">
              <span class="name">All {jobs.length} jobs</span>
              {@render chevron()}
            </summary>
            {#each jobs as job (job.name)}
              <div class="list-row" data-testid="system-job" data-job={job.name} data-outcome={outcome(job)}>
                <span class="dot {troubled(job, now) ? 'bad' : outcome(job) === 'ok' ? 'ok' : 'wait'}"></span>
                <span class="name">{jobLabel(job.name)}</span>
                <span class="value" class:badword={troubled(job, now)}>{said(job)}</span>
              </div>
            {/each}
          </details>
        </div>
      {/if}
    </section>

    <section class="group">
      <h2 class="list-header">More</h2>
      <div class="list-group lines">
        {#if card.logs}
          <details data-testid="system-logs">
            <summary class="list-row">
              <span class="name">Logs</span>
              <span class="value">{LEVELS[level]}</span>
              {@render chevron()}
            </summary>
            <div class="inside">
              <label class="level">
                <span>Show</span>
                <select bind:value={level} aria-label="Log level">
                  {#each Object.entries(LEVELS) as [value, label] (value)}
                    <option {value}>{label}</option>
                  {/each}
                </select>
              </label>
              <p class="footnote">
                The last 200 lines this {card.logs.scope ?? 'web process'} wrote since
                {stamp(card.logs.since) ?? 'it started'}; a restart empties them. The background
                worker's lines are in its container log.
              </p>
              {#if lines.length === 0}
                <p class="footnote" data-empty="logs">Nothing at this level since it started.</p>
              {:else}
                <ol class="logs">
                  {#each lines as line, i (i)}
                    <li class="code" data-log-level={line.level}>
                      <span class="meta">{stamp(line.at)} {line.level} {line.logger}</span>
                      <span>{line.message}</span>
                    </li>
                  {/each}
                </ol>
              {/if}
            </div>
          </details>
        {/if}
        <details data-testid="system-technical">
          <summary class="list-row">
            <span class="name">Technical details</span>
            {@render chevron()}
          </summary>
          <div class="inside">
            <!-- Which key is loaded, never what it is: the stored credential is admin-equivalent. -->
            <h3 class="footnote">Encryption key</h3>
            <p class="code block" data-testid="system-secrets-detail">
              {#if card.secrets.configured}
                SECRETS_KEY fingerprint {card.secrets.fingerprint} · active key_id {card.secrets
                  .key_id ?? 'none yet'}
              {:else}
                SECRETS_KEY is not set
              {/if}
            </p>

            <h3 class="footnote">Jobs</h3>
            <ul class="code block plain">
              {#each jobs as job (job.name)}
                <li>
                  {job.name} · {outcome(job)} · {stamp(finished(job))}{detail(job) ? ` · ${detail(job)}` : ''}
                </li>
              {:else}
                <li>no job_run row yet</li>
              {/each}
            </ul>

            {#if card.queue}
              <h3 class="footnote">Work queue</h3>
              <div class="code block" data-testid="system-queue-detail">
                <p>{STATES.map((s) => `${s} ${card.queue.by_state?.[s] ?? 0}`).join(' · ')}</p>
                {#each kinds(card.queue.by_kind) as row (row.kind)}
                  <p data-queue-kind={row.kind}>{row.kind}: {row.line}</p>
                {:else}
                  <p data-empty="queue">nothing filed yet</p>
                {/each}
              </div>
            {/if}

            {#if card.last_syncs}
              <!-- The newest successful run, which the jobs' newest attempt cannot say. -->
              <h3 class="footnote">Last successful syncs</h3>
              <ul class="code block plain" data-testid="system-last_syncs">
                {#each card.last_syncs as sync (sync.name)}
                  <li data-last-sync={sync.name} data-synced={sync.at ? 'yes' : 'never'}>
                    {sync.connector} · {sync.name} · {sync.at
                      ? `${stamp(sync.at)} · ${ago(sync.at, now)}`
                      : 'never succeeded'}
                  </li>
                {/each}
              </ul>
            {/if}
          </div>
        </details>
      </div>
    </section>
  {/if}
</div>

<style>
  .system {
    display: flex;
    flex-direction: column;
    gap: 24px;
  }
  .err {
    margin: 0;
    color: var(--negative);
    font-size: var(--fs-subhead);
  }
  .group {
    display: flex;
    flex-direction: column;
  }
  .lines > * + *,
  .lines details > * + * {
    box-shadow: inset 0 0.5px 0 var(--separator);
  }
  .list-row {
    color: var(--text);
  }
  .text {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
  }
  .sub {
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-3);
    font-variant-numeric: tabular-nums;
  }
  .name {
    flex: 1;
  }
  .value {
    color: var(--text-3);
    text-align: right;
    font-variant-numeric: tabular-nums;
  }
  .value.badword {
    color: var(--negative);
  }
  .chev {
    display: grid;
    flex: none;
    color: rgba(245, 240, 232, 0.35);
    transition: transform 0.2s var(--ease);
  }
  details[open] > summary .chev {
    transform: rotate(90deg);
  }
  summary {
    cursor: pointer;
    list-style: none;
  }
  summary::-webkit-details-marker {
    display: none;
  }
  .dot {
    flex: none;
    width: 8px;
    height: 8px;
    border-radius: var(--r-pill);
  }
  .dot.ok {
    background: var(--positive);
  }
  .dot.bad {
    background: var(--negative);
  }
  .dot.wait {
    background: var(--text-3);
  }
  .alert {
    display: flex;
    gap: 12px;
    margin: 8px 0 0;
    padding: 12px var(--gutter);
    border-radius: var(--r-md);
    background: var(--warning-tint);
    font-size: var(--fs-subhead);
    line-height: 20px;
    overflow-wrap: anywhere;
  }
  .alert > :global(svg) {
    color: var(--warning);
  }
  .block {
    display: block;
    margin: 0;
    padding: 12px;
    border-radius: var(--r-sm);
    background: var(--bg-elevated);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .alert .block {
    margin-top: 8px;
  }
  .plain {
    list-style: none;
  }
  .block p {
    margin: 0;
  }
  .list-footer.first {
    padding-top: 0;
  }
  .stats {
    display: grid;
    grid-auto-flow: column;
    grid-auto-columns: minmax(0, 1fr);
    gap: 8px;
  }
  .stat {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 12px;
    border-radius: var(--r-md);
    background: var(--surface-1);
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-3);
  }
  .stat.bad {
    color: var(--negative);
  }
  .figure {
    font-size: var(--fs-section);
    line-height: 25px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    color: var(--text);
  }
  .inside {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 4px var(--gutter) var(--gutter);
  }
  .inside h3 {
    margin: 8px 0 0;
    font-weight: 600;
  }
  .level {
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: var(--fs-footnote);
    color: var(--text-3);
  }
  .footnote {
    margin: 0;
  }
  .logs {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .logs li {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 8px 12px;
    border-radius: var(--r-sm);
    background: var(--bg-elevated);
    overflow-wrap: anywhere;
  }
  .logs li[data-log-level='ERROR'],
  .logs li[data-log-level='CRITICAL'] {
    color: var(--text);
  }
  .meta {
    color: var(--text-3);
  }

  @media (min-width: 721px) {
    .system {
      max-width: 680px;
    }
  }
</style>
