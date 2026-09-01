<script>
  /**
   * Bundle import. Spec v2.1 §10 swap sequence, §6.6 Data tab, §3.1 wizard step 3 —
   * one component, two entry points, because §3.1 says it is "the same importer".
   *
   * The report is shown in full rather than reduced to a green tick: §10 calls it a migration
   * report, and the counts are the diff material for the next re-import.
   */
  import { post } from '$lib/api.js';

  let { onImported = () => {} } = $props();

  let path = $state('');
  let busy = $state(false);
  let phase = $state('idle'); // idle | validated | imported | failed
  let report = $state(null);
  let error = $state('');

  const bySeverity = (sev) => (report?.findings ?? []).filter((f) => f.severity === sev);
  const glyph = { fail: '×', warn: '!', note: '✓' };

  async function run(endpoint) {
    busy = true;
    error = '';
    try {
      const res = await post(`/admin/bundle/${endpoint}`, { path: path || null });
      report = res.report;
      phase = endpoint === 'validate' ? 'validated' : 'imported';
      if (endpoint === 'import') onImported(res);
    } catch (err) {
      error = err.message;
      report = err.detail?.report ?? null;
      phase = 'failed';
    } finally {
      busy = false;
    }
  }
</script>

<div class="box">
  <div class="steps">
    {#each ['validate', 'report', 'swap', 'active'] as s, i (s)}
      <span
        class="step data"
        class:on={(phase === 'validated' && i <= 1) || (phase === 'imported' && i <= 3)}
      >{s}</span>
    {/each}
  </div>

  <label>
    <span class="data">BUNDLE PATH · DEFAULTS TO /data/import</span>
    <input type="text" bind:value={path} placeholder="/data/import" />
  </label>

  <div class="row">
    <button class="btn-ghost" onclick={() => run('validate')} disabled={busy}>
      {busy ? 'Working…' : 'Validate bundle'}
    </button>
    <button
      class="btn-primary"
      onclick={() => run('import')}
      disabled={busy || phase === 'idle' || (report && !report.ok)}
    >
      Import and activate
    </button>
  </div>

  {#if error && !report}<div class="err">{error}</div>{/if}

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

  {#if phase === 'imported'}
    <p class="why">
      Restart backend and worker — no process may score or refit with a loaded bundle version
      different from the active row.
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
  .step.on {
    border-color: var(--ember);
    color: var(--ember-lift);
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
    /* A validation strip nested under the file input, never alone on screen: on
       /admin/data it renders directly beneath `.bundle-active`, which is this size. */
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
  .finding {
    display: grid;
    grid-template-columns: 14px 150px 1fr;
    gap: 8px;
    align-items: baseline;
    font-size: 12.5px;
    line-height: 1.45;
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
</style>
