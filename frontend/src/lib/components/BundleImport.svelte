<script>
  // Import is a worker job: the report shown after a press is the one the worker stored on its
  // `job_run` row, polled from `/admin/bundle/state`, not the 202's validation report.
  import { onDestroy, onMount } from 'svelte';
  import { get, post } from '$lib/api.js';
  import { session } from '$lib/session.svelte.js';
  import {
    FAILED,
    IDLE,
    IMPORTED,
    LIVE,
    POLL_READ_TIMEOUT_MS,
    RESTART,
    RUNNING,
    UNKNOWN,
    UNKNOWN_OUTCOME,
    VALIDATED,
    detailPairs,
    importDisabled,
    phaseForImportError,
    phaseOfJob,
    pollImportJob,
    servedAfterImport,
    stepsLit
  } from '$lib/bundleImport.svelte.js';

  // No default: `null` ("no import") and absent ("nobody said") differ, and the wizard passes none.
  /** @type {{ onImported?: (outcome?: any) => void, importJob?: any }} */
  let { onImported = () => {}, importJob } = $props();

  let path = $state('');
  let busy = $state(false);
  let phase = $state(IDLE); // idle | validated | running | imported | failed | unknown
  let report = $state(null);
  let error = $state('');
  // After IMPORTED: served (LIVE), owed a restart (RESTART) or not said (null). Decision 497.
  let served = $state(null);
  let servedVersion = $state('');
  // Set on destroy and read by the polls; a plain let, since nothing renders it.
  let gone = false;

  const bySeverity = (sev) => (report?.findings ?? []).filter((f) => f.severity === sev);
  const SEVERITY = { fail: 'Fails', warn: 'Warning', note: 'Fine' };
  const ICON = {
    fail: 'M6.5 6.5l11 11M17.5 6.5l-11 11',
    warn: 'M12 5v9M12 19v.01',
    note: 'm5 12.5 4.5 4.5L19 7.5'
  };

  async function run(endpoint) {
    busy = true;
    error = '';
    try {
      const res = await post(`/admin/bundle/${endpoint}`, { path: path || null });
      report = res.report;
      if (endpoint === 'validate') {
        phase = VALIDATED;
        return;
      }
      // The 202's report shows while the worker runs, then the stored one replaces it.
      await watch(res.job_id);
    } catch (err) {
      // A 422 is a report (`detail.report`); every other refusal is a sentence. Exactly one is set.
      const refused = err.detail?.report ?? null;
      error = refused ? '' : err.message;
      // A failed import keeps the validation report, the last true thing this page knows.
      report = refused ?? (endpoint === 'validate' ? null : report);
      // A validate writes nothing, so only an import can end in an unknown outcome.
      phase = endpoint === 'validate' ? FAILED : phaseForImportError(err);
    } finally {
      busy = false;
    }
  }

  // Watches a queued or running job to its end, whether pressed here or adopted on load. Svelte
  // does not stop a pending poll on destroy, so the poll reads `gone` between its reads.
  async function watch(jobId) {
    phase = RUNNING;
    const outcome = await pollImportJob(
      // A deadline per read: `api.js` bounds none, and a hung read would pin the screen on running.
      () => get('/admin/bundle/state', { signal: AbortSignal.timeout(POLL_READ_TIMEOUT_MS) }),
      jobId,
      { abandoned: () => gone }
    );
    if (!outcome) return;
    phase = outcome.phase;
    if (outcome.job?.report) report = outcome.job.report;
    if (outcome.error) error = outcome.error;
    served = phase === IMPORTED ? servedAfterImport(outcome.state) : null;
    servedVersion = outcome.state?.active ?? outcome.job?.bundle_version ?? '';
    if (phase === IMPORTED) onImported(outcome);
  }

  onDestroy(() => {
    gone = true;
  });

  // Adopt an import this tab did not start (a reload mid-import, another device, the wizard).
  // Only a RUNNING row, or every visit would re-poll; only for an admin, and silent otherwise.
  onMount(async () => {
    const job = importJob === undefined ? await ownState() : importJob;
    if (phaseOfJob(job) !== RUNNING) return;
    watch(job.job_id).catch((err) => {
      error = err?.message ?? String(err);
      phase = UNKNOWN;
    });
  });

  async function ownState() {
    if (session.user?.role !== 'admin') return null;
    try {
      return (await get('/admin/bundle/state')).import_job ?? null;
    } catch {
      return null;
    }
  }
</script>

<div class="box" data-phase={phase}>
  <ol class="steps" aria-label="Import steps">
    {#each ['Validate', 'Report', 'Swap', 'Live'] as s, i (s)}
      <li class="step" class:on={i < stepsLit(phase, !!report)}>{s}</li>
    {/each}
  </ol>

  <label class="path">
    <span class="label">Where the bundle is</span>
    <input type="text" bind:value={path} placeholder="/data/import" />
    <span class="footnote">
      A bundle folder or a .tar or .tar.zst file. Leave it empty for /data/import.
    </span>
  </label>

  <div class="row">
    <!-- Also dark while RUNNING: an adopted watch sets the phase without `busy`, and a validate
         mid-swap would overwrite it. -->
    <button class="btn-secondary" onclick={() => run('validate')} disabled={busy || phase === RUNNING}>
      {busy ? 'Working…' : 'Validate bundle'}
    </button>
    <button
      class="btn-primary"
      onclick={() => run('import')}
      disabled={importDisabled({ busy, phase, report })}
    >
      Import and activate
    </button>
  </div>

  <!-- A refusal that carries a report sets no sentence, so this never repeats the report. -->
  {#if error}<p class="err">{error}</p>{/if}

  {#if phase === UNKNOWN}
    <!-- The button stays dark: the 202 may have queued a job this tab lost track of. -->
    <p class="err" data-unknown>{UNKNOWN_OUTCOME}</p>
  {/if}

  {#if report}
    <div class="report">
      <div class="head">
        <span class="version">
          {report.bundle_version ?? 'Unknown version'}
          <span class="footnote">vocabulary {report.vocabulary_version ?? 'unknown'}</span>
        </span>
        <!-- Capitalised by CSS: the word itself is what scripts read. -->
        <span class="verdict badge" class:ok={report.ok} class:bad={!report.ok}>
          {report.ok ? 'valid' : 'rejected'}
        </span>
      </div>

      <ul class="findings">
        {#each ['fail', 'warn', 'note'] as sev}
          {#each bySeverity(sev) as f}
            <li class="finding {sev}">
              <svg class="g" viewBox="0 0 24 24" role="img" aria-label={SEVERITY[sev]}>
                <path d={ICON[sev]} />
              </svg>
              <span class="body">
                <span class="msg">{f.message}</span>
                <span class="rule code">{f.rule}</span>
                <!-- A failure's detail names its tables or columns, as the report renders it. -->
                {#if sev === 'fail'}
                  {#each detailPairs(f) as [key, line] (key)}
                    <span class="detail code"><span class="key">{key}</span> {line}</span>
                  {/each}
                {/if}
              </span>
            </li>
          {/each}
        {/each}
      </ul>

      {#if Object.keys(report.counts ?? {}).length}
        <details>
          <summary>Row counts</summary>
          <div class="counts">
            {#each Object.entries(report.counts).sort() as [table, n] (table)}
              <div class="count"><span class="code">{table}</span><span class="data">{n.toLocaleString()}</span></div>
            {/each}
          </div>
        </details>
      {/if}

      {#if Object.keys(report.unmapped_columns ?? {}).length}
        <details>
          <summary>Columns the importer does not read</summary>
          <div class="counts">
            {#each Object.entries(report.unmapped_columns) as [table, cols] (table)}
              <div class="count"><span class="code">{table}</span><span class="code">{cols.join(', ')}</span></div>
            {/each}
          </div>
          <p class="footnote">
            Reported rather than dropped silently: the corpus export is the authority on its own
            column names.
          </p>
        </details>
      {/if}
    </div>
  {/if}

  <!-- The backend loads the flipped bundle itself; the command shows only when a restart is owed. -->
  {#if phase === IMPORTED && served === LIVE}
    <p class="why" data-served={LIVE}>
      Movie data {servedVersion} is live. Nothing needs restarting.
    </p>
  {:else if phase === IMPORTED && served === RESTART}
    <p class="err" data-served={RESTART}>
      Movie data {servedVersion} is imported, but the backend could not load it by itself (its log
      says why). Restart backend and worker: <code class="code">docker compose restart backend worker</code>
    </p>
  {/if}
</div>

<style>
  .box {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  .steps {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 4px;
  }
  /* Progress in neutral light, not the accent: nothing here is selected. */
  .step {
    padding-top: 8px;
    border-top: 3px solid var(--surface-3);
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-3);
  }
  .step.on {
    border-top-color: var(--text);
    color: var(--text);
  }
  .path {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .label {
    padding: 0 4px;
    font-size: var(--fs-subhead);
    line-height: 20px;
    color: var(--text-2);
  }
  .path .footnote {
    padding: 0 4px;
  }
  .row {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .row button {
    flex: 1 1 220px;
  }
  .report {
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: var(--card-pad);
    border-radius: var(--r-md);
    background: var(--surface-1);
  }
  .head {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 12px;
  }
  .version {
    display: flex;
    flex-direction: column;
    font-size: var(--fs-body);
    line-height: 22px;
    font-variant-numeric: tabular-nums;
    overflow-wrap: anywhere;
  }
  .verdict {
    flex: none;
    text-transform: capitalize;
  }
  .findings {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .finding {
    display: flex;
    gap: 10px;
    align-items: flex-start;
  }
  .g {
    flex: none;
    width: 18px;
    height: 18px;
    margin-top: 1px;
    fill: none;
    stroke: currentColor;
    stroke-width: 2;
    stroke-linecap: round;
    stroke-linejoin: round;
  }
  .finding.fail .g {
    color: var(--negative);
  }
  .finding.warn .g {
    color: var(--warning);
  }
  .finding.note .g {
    color: var(--positive);
  }
  /* Messages carry unbreakable paths, so the column must be able to shrink and wrap. */
  .body {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
    overflow-wrap: anywhere;
  }
  .msg {
    font-size: var(--fs-subhead);
    line-height: 20px;
    color: var(--text);
  }
  .rule,
  .detail {
    color: var(--text-3);
  }
  .key {
    color: var(--text-2);
  }
  details {
    border-top: 0.5px solid var(--separator);
    padding-top: 8px;
  }
  summary {
    min-height: 44px;
    display: flex;
    align-items: center;
    font-size: var(--fs-subhead);
    color: var(--text-2);
    cursor: pointer;
  }
  .counts {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .count {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    overflow-wrap: anywhere;
  }
  details .footnote {
    margin: 8px 0 0;
  }
  .why {
    margin: 0;
  }
  .err {
    margin: 0;
    color: var(--negative);
    font-size: var(--fs-subhead);
    line-height: 20px;
    overflow-wrap: anywhere;
  }
</style>
