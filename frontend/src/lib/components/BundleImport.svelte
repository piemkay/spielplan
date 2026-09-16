<script>
  /**
   * Bundle import. Spec v2.1 §10 swap sequence, §6.6 Data tab, §3.1 wizard step 3 —
   * one component, two entry points, because §3.1 says it is "the same importer".
   *
   * The report is shown in full rather than reduced to a green tick: §10 calls it a migration
   * report, and the counts are the diff material for the next re-import.
   *
   * THE IMPORT IS NO LONGER THIS REQUEST'S TO WAIT FOR. §5.3 files it as a job with a minutes
   * budget, and M4.14 moved it there: `POST /admin/bundle/import` answers 202 with a `job_id`
   * the moment validation passes, and the load, the rebuild set and the flip happen in the
   * worker. So the report this screen shows after a press is the one the WORKER stored on its
   * `job_run` row (decision 253), fetched by polling `/admin/bundle/state` — not the validation
   * report the 202 carried, which describes the decision and not the outcome.
   *
   * The phase machine and the button's rule live in `$lib/bundleImport.svelte.js`; the argument
   * for that is there. What is here is the screen. [M4.14 step E4, findings 2.1, 2.3, 2.22]
   */
  import { onDestroy, onMount } from 'svelte';
  import { get, post } from '$lib/api.js';
  import { session } from '$lib/session.svelte.js';
  import {
    FAILED,
    IDLE,
    IMPORTED,
    POLL_READ_TIMEOUT_MS,
    RUNNING,
    UNKNOWN,
    UNKNOWN_OUTCOME,
    VALIDATED,
    detailPairs,
    importDisabled,
    phaseForImportError,
    phaseOfJob,
    pollImportJob,
    stepsLit
  } from '$lib/bundleImport.svelte.js';

  // `importJob` is `GET /admin/bundle/state`'s newest bundle-import row, whatever its phase, or
  // `null`. The page hands it down and this component decides what to do with it; see `onMount`.
  // No default, so that "the page said there is no import" (`null`) and "nobody said" (absent)
  // stay two different answers -- which is the whole of the wizard's half below. Typed here
  // because a prop with no default is a REQUIRED prop to `svelte-check`, and §3.1's wizard is
  // the caller that deliberately passes none.
  /** @type {{ onImported?: (outcome?: any) => void, importJob?: any }} */
  let { onImported = () => {}, importJob } = $props();

  let path = $state('');
  let busy = $state(false);
  let phase = $state(IDLE); // idle | validated | running | imported | failed | unknown
  let report = $state(null);
  let error = $state('');
  // Whether this screen still exists; see `watch`. A plain `let` and not a rune: nothing renders
  // it, and the only things that read it are the two closures below.
  let gone = false;

  const bySeverity = (sev) => (report?.findings ?? []).filter((f) => f.severity === sev);
  // Stays a glyph map: this is a browser rendering one character per finding, where the cost of
  // `×` over `x` is nothing. The ASCII rule CLAUDE.md states is about what a Windows console
  // prints, and the report's own `render()` spends `-` and `+` there for exactly that reason.
  // [M4.14 finding 2.24]
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
      // The 202's own report is the validation that let the job be queued; it is shown while the
      // worker runs so the panel does not blank, and then replaced by the stored one.
      await watch(res.job_id);
    } catch (err) {
      // A 422 IS a report - `api/artifacts.py` sends `detail={report, text}` - and it replaces
      // whatever was on screen. Every other refusal carries a sentence instead, and the two are
      // rendered in different places, so exactly one of them is set.
      const refused = err.detail?.report ?? null;
      error = refused ? '' : err.message;
      // On an import that died in transit the validation report STAYS. It is the last true thing
      // this page knows - the operator needs to see what they pressed Import on - and blanking
      // the panel at the one moment nobody knows what the install is doing is the opposite of
      // what section 10 asks a migration report to be. A validate is the other way round: a
      // refused path describes a different attempt, so its report goes.
      report = refused ?? (endpoint === 'validate' ? null : report);
      // A validate writes nothing, so a validate that failed left the install exactly as it was
      // and there is nothing to be unknown about. An import is the other case, and
      // `phaseForImportError` is where the two halves of "the server answered" are told apart.
      phase = endpoint === 'validate' ? FAILED : phaseForImportError(err);
    } finally {
      busy = false;
    }
  }

  /**
   * Watch a queued or running `job_run` row to its end, and show what the worker stored on it.
   *
   * One function for the two ways this screen comes to be watching an import: the one it just
   * pressed Import on, and one that was already running when the page loaded. Both end the same
   * way because the phase and the report are the SERVER's -- decision 253 puts them on the row --
   * and a screen with one account of its own import and another of an adopted one would be two
   * answers to what the same install did. [M4.14 step E4]
   *
   * A POLL IS AN ASYNC FUNCTION AND SVELTE DESTROYING THIS COMPONENT DOES NOT STOP ONE. An
   * operator who clicked Connectors during the 127 s load left a poll reading
   * `/admin/bundle/state` every two seconds until its eleven-minute deadline, writing `phase`
   * into a destroyed screen and firing `onImported` -- which on the Data tab re-bootstraps the
   * shared session store, minutes later, over whatever page they are now looking at. Coming back
   * to the tab added a second poll to the first rather than replacing it.
   *
   * `gone` is read by the poll rather than checked once here, because a poll sleeps two seconds
   * between reads and the screen can go in that window. It is also what covers the two round
   * trips that can outlive the screen and then START a watch -- the adopted row below and this
   * screen's own 202, both of which reach `watch` after an await.
   * [M4.14 cycle 4, m414-c4-dim202-02]
   */
  async function watch(jobId) {
    phase = RUNNING;
    const outcome = await pollImportJob(
      // A deadline per read and not only per poll: `api.js` bounds neither, and a read that
      // never settles is a screen stuck on `running` past the point the worker's own timeout
      // and the reaper have closed the row. [M4.14 cycle 4, m414-c4-dim202-03]
      () => get('/admin/bundle/state', { signal: AbortSignal.timeout(POLL_READ_TIMEOUT_MS) }),
      jobId,
      { abandoned: () => gone }
    );
    if (!outcome) return;
    phase = outcome.phase;
    if (outcome.job?.report) report = outcome.job.report;
    if (outcome.error) error = outcome.error;
    if (phase === IMPORTED) onImported(outcome);
  }

  // The one line that ends every poll this component started.
  onDestroy(() => {
    gone = true;
  });

  /**
   * An import this tab did not start. [M4.14 review cycle 1, waveE-06]
   *
   * M4.14 made the import outlive the request that queued it, so `/admin/bundle/state` can answer
   * `running` on a page load that pressed nothing -- an operator who reloaded during the 127 s
   * load, or who opened the Data tab on a second device -- and §6.6 makes this page where they
   * find out. `import_job` was added to that payload in this milestone (decision 253) for a
   * reader nobody wrote: until this, such a load showed an idle wizard and an armable Validate
   * while the worker was mid-swap, which is finding 2.1's shape on the operator's instrument.
   *
   * `phaseOfJob` and not `if (importJob)`: the newest row outlives the import forever, so
   * adopting any row at all would re-poll and re-fire `onImported` on every visit to this tab for
   * the life of the install. `onMount` and not an `$effect`: the page renders this component only
   * once `/admin/bundle/state` has answered, so the row is here at mount, and a later change to
   * this prop is the refresh that follows this screen's OWN import -- which it has just watched.
   *
   * AND THE WIZARD GETS THE SAME ADOPTION, which is why the row is fetched here when no one
   * handed one down. §3.1 calls its step 3 "the same importer the §6.6 Data tab exposes" and
   * mounts this component with no prop, while the wizard itself reads `/setup/state` only -- so
   * the surface where the measured 127 s import actually happens was the one surface that could
   * not adopt it. An operator cut off by §2's 100 s proxy, or one who pressed F5, came back to
   * `/setup` (which `+layout.svelte` keeps reachable for a signed-in admin) and found an idle
   * step 3 with Validate armed over a worker mid-swap: verbatim the lying screen cycle 1's
   * waveE-06 repaired on the Data tab. Stated here rather than in the wizard so that which rows
   * are worth adopting stays said once, where the phase names are.
   *
   * Only for an admin, and never louder than a no-op: `/admin/bundle/state` is `AdminUser`-gated
   * and step 0 of the wizard is reachable signed out, so a read this operator did not ask for
   * must not put a 401 on the one screen §3.1 gives them. Nothing to adopt is this screen's
   * ordinary state, not an error about it. [M4.14 cycle 4, m414-c4-dim202-01]
   */
  onMount(async () => {
    const job = importJob === undefined ? await ownState() : importJob;
    // `gone`: the read above is a round trip, and a screen destroyed while it was in flight has
    // already had its one chance to end what this line would start.
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

  <!-- Decision 257 taught the default path to find a single `.tar` / `.tar.zst` sitting in
       `/data/import` and to say so as a report note, which is how a bundle actually arrives on
       the box — the corpus ships one archive, and unpacking it by hand first was a step the
       label invented. The field says what it accepts now that both shapes reach the importer.
       [M4.14 step A5] -->
  <label>
    <span class="data">BUNDLE PATH · A BUNDLE DIRECTORY OR .TAR/.TAR.ZST · DEFAULTS TO /data/import</span>
    <input type="text" bind:value={path} placeholder="/data/import" />
  </label>

  <div class="row">
    <!-- `busy || phase === RUNNING`, and no longer `busy` alone. `busy` is true only while a
         request THIS screen sent is in flight, and the adopted path above watches an import it
         never pressed: `watch()` sets `phase = RUNNING` without touching `busy`, so a reload
         during the 127 s load left this button live for the whole of a swap. Pressing it was not
         harmless. `/admin/bundle/validate` takes no lock and an import in the worker is one
         uncommitted transaction, so it answers ok, and `run('validate')` writes VALIDATED over
         the running phase -- which re-arms "Import and activate" in the state E4 calls
         terminal-until-polled, drops the strip from three lit steps to two mid-swap, and paints a
         409 "already running" over an import that is succeeding. A watch in flight is this
         screen's one writer of `phase`. [M4.14 cycle 2, waveE-03] -->
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

  <!-- `{#if error}` and no longer `{#if error && !report}`. The guard was there to stop a 422
       printing its whole rendered report twice, which is now settled where the error is set: a
       refusal that carries a report sets no sentence. With the old guard, any refusal arriving
       while a report happened to be on screen - a 409 "another bundle import is already running",
       a 400 naming the path - was swallowed silently, and after M4.14 an import that keeps its
       validation report is the ordinary case rather than the rare one. -->
  {#if error}<div class="err">{error}</div>{/if}

  {#if phase === UNKNOWN}
    <!-- The sixth state, and the reason the button above stays dark rather than re-arming: the
         202 may have queued a job this tab then lost, and the worker acts on the row whatever
         this page believes. Saying "failed" here is what §2's 100 s proxy cut already made this
         screen say about imports that completed. [M4.14 findings 2.1 and 2.3] -->
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
          <!-- A failure's `detail` is where it says WHICH — the denied tables, the columns the
               bundle lacks, the first orphan ids — and this page rendered the message alone, so
               rule 7's "3 denied table(s)" reached the one person who has to go and fix them with
               nothing named. Failures only: a clean import already renders fourteen findings and
               only a failure is a surface anyone has to act on, which is the same line
               `ImportReport.render()` draws. [M4.14 finding 2.22] -->
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

  {#if phase === IMPORTED}
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
  .detail {
    /* Indented under the failure it belongs to and one column narrower: the rule column is what
       the eye follows down the report, and a detail key is not a rule. The 22px indent is the
       glyph column above it, so the keys line up under the message they qualify. */
    display: grid;
    grid-template-columns: 140px 1fr;
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
</style>
