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
  // A glyph map is fine here: the ASCII rule is for Windows consoles, not the browser.
  const glyph = { fail: '×', warn: '!', note: '✓' };

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
  <div class="steps">
    {#each ['validate', 'report', 'swap', 'active'] as s, i (s)}
      <span class="step data" class:on={i < stepsLit(phase, !!report)}>{s}</span>
    {/each}
  </div>

  <label>
    <span class="data">BUNDLE PATH · A BUNDLE DIRECTORY OR .TAR/.TAR.ZST · DEFAULTS TO /data/import</span>
    <input type="text" bind:value={path} placeholder="/data/import" />
  </label>

  <div class="row">
    <!-- Also dark while RUNNING: an adopted watch sets the phase without `busy`, and a validate
         mid-swap would overwrite it. -->
    <button class="btn-ghost" onclick={() => run('validate')} disabled={busy || phase === RUNNING}>
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
  {#if error}<div class="err">{error}</div>{/if}

  {#if phase === UNKNOWN}
    <!-- The button stays dark: the 202 may have queued a job this tab lost track of. -->
    <div class="err" data-unknown>{UNKNOWN_OUTCOME}</div>
  {/if}

  {#if report}
    <div class="report card">
      <div class="head">
        <span class="data-lg">
          bundle {report.bundle_version ?? '(unknown)'} · vocabulary
          {report.vocabulary_version ?? '(unknown)'}
        </span>
        <span class="verdict" class:bad={!report.ok}>{report.ok ? 'valid' : 'rejected'}</span>
      </div>

      {#each ['fail', 'warn', 'note'] as sev}
        {#each bySeverity(sev) as f}
          <div class="finding {sev}">
            <span class="g">{glyph[sev]}</span>
            <span class="rule data">{f.rule}</span>
            <span class="msg">{f.message}</span>
          </div>
          <!-- A failure's detail names the tables or columns (failures only, as the report renders). -->
          {#if sev === 'fail'}
            {#each detailPairs(f) as [key, line] (key)}
              <div class="detail"><span class="data">{key}</span><span class="msg">{line}</span></div>
            {/each}
          {/if}
        {/each}
      {/each}

      {#if Object.keys(report.counts ?? {}).length}
        <details>
          <summary class="data">ROW COUNTS</summary>
          <div class="counts">
            {#each Object.entries(report.counts).sort() as [table, n] (table)}
              <div class="count"><span class="data">{table}</span><span class="data">{n.toLocaleString()}</span></div>
            {/each}
          </div>
        </details>
      {/if}

      {#if Object.keys(report.unmapped_columns ?? {}).length}
        <details>
          <summary class="data">UNMAPPED BUNDLE COLUMNS</summary>
          <div class="counts">
            {#each Object.entries(report.unmapped_columns) as [table, cols] (table)}
              <div class="count"><span class="data">{table}</span><span class="data">{cols.join(', ')}</span></div>
            {/each}
          </div>
          <p class="why">
            Reported, not dropped silently — the corpus export is the authority on its own
            column names.
          </p>
        </details>
      {/if}
    </div>
  {/if}

  <!-- The backend loads the flipped bundle itself; the command shows only when a restart is owed. -->
  {#if phase === IMPORTED && served === LIVE}
    <p class="why" data-served={LIVE}>
      Bundle {servedVersion} is live. Nothing needs restarting.
    </p>
  {:else if phase === IMPORTED && served === RESTART}
    <p class="err" data-served={RESTART}>
      Bundle {servedVersion} is imported, but the backend could not load it by itself (its log
      says why). Restart backend and worker: <code>docker compose restart backend worker</code>
    </p>
  {/if}
</div>

<style>
  .box {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .steps {
    display: flex;
    gap: 6px;
  }
  .step {
    flex: 1;
    text-align: center;
    padding: 8px;
    border: 1px solid var(--line-2);
    border-radius: var(--r-sm);
  }
  /* The progress ramp, not the accent: nothing here is selected (§6.8). */
  .step.on {
    border-color: var(--progress-now);
    color: var(--ink);
  }
  label {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .row {
    display: flex;
    gap: 8px;
  }
  .report {
    padding: var(--card-pad-tight);
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .head {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    border-bottom: 1px solid var(--line);
    padding-bottom: 8px;
    margin-bottom: 4px;
  }
  .verdict {
    font-family: var(--mono);
    font-size: 10px;
    color: #5fae7a;
  }
  .verdict.bad {
    color: var(--ember-lift);
  }
  /* Messages carry unbreakable paths, so the column must be able to shrink and wrap. */
  .finding {
    display: grid;
    grid-template-columns: 14px minmax(0, 150px) minmax(0, 1fr);
    gap: 8px;
    align-items: baseline;
    font-size: 12.5px;
    line-height: 1.45;
  }
  .finding .msg,
  .finding .rule,
  .detail .msg,
  .err {
    overflow-wrap: anywhere;
  }
  .finding.fail .g,
  .finding.fail .msg {
    color: var(--ember-lift);
  }
  .finding.warn .g {
    color: #c9a227;
  }
  .finding.note .g {
    color: #5fae7a;
  }
  .detail {
    /* Indented under its failure, so the keys line up under the message they qualify. */
    display: grid;
    grid-template-columns: 140px minmax(0, 1fr);
    gap: 8px;
    align-items: baseline;
    font-size: 12px;
    line-height: 1.45;
    padding-left: 30px;
  }
  .msg {
    color: var(--ink-2);
  }
  .counts {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding-top: 6px;
  }
  .count {
    display: flex;
    justify-content: space-between;
    gap: 12px;
  }
  summary {
    cursor: pointer;
    padding-top: 6px;
  }
  .err {
    color: var(--ember-lift);
    font-size: 12.5px;
  }
  /* Phone: the message goes under its rule, full width; last, so it wins. */
  @media (max-width: 480px) {
    .finding {
      grid-template-columns: 14px minmax(0, 1fr);
    }
    .finding .msg {
      grid-column: 2;
    }
    .detail {
      grid-template-columns: minmax(0, 96px) minmax(0, 1fr);
      padding-left: 22px;
    }
  }
</style>
