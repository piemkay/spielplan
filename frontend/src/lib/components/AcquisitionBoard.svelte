<script>
  /**
   * §6.6 Data's acquisition board. Spec v2.1 §6.6 Data, §8; decisions 336, 345, 424, 444.
   *
   * One card, one component, in `BundleImport.svelte`'s pattern: it fetches its own envelope, so a
   * board that cannot load says so here and never takes the bundle wizard above it down.
   *
   * THE REASON IS THE PRODUCT. `0005_ledger.sql` comments `acquisition_job.reason` "shown verbatim
   * on the admin board", and the stage that parked a job wrote that sentence for the operator: it
   * is printed whole, wrapped rather than cut, and a refusal from an action is printed the same way
   * beside the job it refused. If a reason reads badly, the fix is in its writer (plan section 9).
   *
   * PARKED IS NOT BROKEN (decision 336). The placement sweep has parked thin-but-placed titles at
   * stage 2 since M4.13 - the title is ready, the job waits for enrichment - so parked, failed and
   * abandoned are three tones and three sentences, and each job offers only the actions its state
   * admits, as the server lists them in `actions`.
   *
   * A JOB SHOWS ITS DOCUMENTS, NEVER THE BYTES (decision 345). The disclosure lists the
   * `raw_document` metadata the detail route returns, with the URL as text and not as a link: the
   * bytes are on the worker's volume alone, and a link would be a door the backend cannot open.
   */
  import { onMount } from 'svelte';
  import { api, get } from '$lib/api.js';
  import {
    ABANDON,
    RETRY,
    RETRY_FROM,
    actionRequest,
    documentFacts,
    offered,
    refusalOf,
    retryStages,
    segments,
    stageLabel,
    statusOf
  } from '$lib/acquisitionBoard.svelte.js';

  let board = $state(null);
  let error = $state('');
  // Per job, keyed by title id, because each row acts on its own and a refusal belongs beside the
  // job it refused rather than in one line at the top of a list of two hundred.
  let refusals = $state({});
  let busy = $state({});
  let fromStage = $state({});
  let confirming = $state({});
  let open = $state({});
  let details = $state({});

  const stages = $derived(board?.stages ?? []);
  const jobs = $derived(board?.jobs ?? []);

  async function refresh() {
    try {
      board = await get('/admin/acquisition');
      error = '';
    } catch (err) {
      error = err.message;
    }
  }

  async function act(job, action) {
    const id = job.title_id;
    const { path, body } = actionRequest(action, id, fromStage[id] ?? job.stage);
    busy[id] = true;
    confirming[id] = false;
    try {
      await api(path, { method: 'POST', body });
      refusals[id] = '';
      // The job may stand at another stage now, and a picked stage past it is not one to offer.
      fromStage[id] = undefined;
    } catch (err) {
      refusals[id] = refusalOf(err);
    } finally {
      busy[id] = false;
    }
    await refresh();
  }

  async function toggleDocuments(job) {
    const id = job.title_id;
    open[id] = !open[id];
    if (!open[id] || details[id]) return;
    try {
      details[id] = await get(`/admin/acquisition/${id}`);
    } catch (err) {
      details[id] = { error: err.message };
    }
  }

  function titleOf(job) {
    return job.year ? `${job.name} (${job.year})` : job.name;
  }

  onMount(refresh);
</script>

<div class="board" data-testid="acquisition-board">
  <h2>Acquisition</h2>
  <p class="why">
    Every title the pipeline has an opinion about, newest movement first, at the stage it reached
    and with the reason it stopped there.
  </p>

  {#if error}
    <p class="err">{error}</p>
  {:else if !board}
    <p class="data">loading...</p>
  {:else}
    <ol class="legend" data-testid="board-stages">
      {#each stages as stage (stage.number)}
        <li>
          <span class="data">{stage.number}</span>
          <span class="data-lg" data-testid="board-stage">{stage.name}</span>
        </li>
      {/each}
    </ol>

    {#if jobs.length === 0}
      <p class="why">No title is in the pipeline.</p>
    {/if}

    <ul class="jobs">
      {#each jobs as job (job.title_id)}
        {@const status = statusOf(job.status)}
        {@const can = offered(job)}
        <li
          class="job card {status.tone}"
          data-testid="board-job"
          data-title-id={job.title_id}
          data-status={job.status}
        >
          <div class="head">
            <span class="name">{titleOf(job)}</span>
            <span class="data">title {job.title_id} - stage {stageLabel(stages, job)}</span>
          </div>
          <ol class="segments" aria-label="stage {job.stage} of {stages.length}">
            {#each segments(stages, job) as seg (seg.number)}
              <li class="seg {seg.state}" title="{seg.number} {seg.name}"></li>
            {/each}
          </ol>
          <p class="state data-lg" data-testid="board-status">{status.label}</p>
          {#if job.reason != null}
            <p class="reason" data-testid="board-reason">{job.reason}</p>
          {/if}
          {#if job.retry_after}
            <p class="data">retry after {new Date(job.retry_after).toLocaleString()}</p>
          {/if}

          {#if can.retry || can.retryFrom || can.abandon}
            <div class="actions">
              {#if can.retry}
                <button class="btn-ghost" disabled={busy[job.title_id]} onclick={() => act(job, RETRY)}>
                  Retry
                </button>
              {/if}
              {#if can.retryFrom}
                <label class="from">
                  <span class="data">stage</span>
                  <select
                    aria-label="Stage to retry from"
                    value={fromStage[job.title_id] ?? job.stage}
                    onchange={(e) => (fromStage[job.title_id] = Number(e.currentTarget.value))}
                  >
                    {#each retryStages(stages, job) as stage (stage.number)}
                      <option value={stage.number}>{stage.number} {stage.name}</option>
                    {/each}
                  </select>
                </label>
                <button
                  class="btn-ghost"
                  disabled={busy[job.title_id]}
                  onclick={() => act(job, RETRY_FROM)}
                >
                  Retry from stage
                </button>
              {/if}
              {#if can.abandon}
                {#if confirming[job.title_id]}
                  <span class="confirm why">
                    Nothing runs for this title until it is retried from a stage.
                  </span>
                  <button class="btn-ghost" disabled={busy[job.title_id]} onclick={() => act(job, ABANDON)}>
                    Yes, abandon
                  </button>
                  <button class="btn-ghost" onclick={() => (confirming[job.title_id] = false)}>
                    Keep it
                  </button>
                {:else}
                  <button
                    class="btn-ghost"
                    disabled={busy[job.title_id]}
                    onclick={() => (confirming[job.title_id] = true)}
                  >
                    Abandon
                  </button>
                {/if}
              {/if}
            </div>
          {/if}

          {#if refusals[job.title_id]}
            <p class="refusal" data-testid="board-refusal">{refusals[job.title_id]}</p>
          {/if}

          <button
            class="btn-ghost docs-toggle"
            aria-expanded={open[job.title_id] ? 'true' : 'false'}
            onclick={() => toggleDocuments(job)}
          >
            Fetched documents
          </button>
          {#if open[job.title_id]}
            {@const detail = details[job.title_id]}
            {#if !detail}
              <p class="data">loading...</p>
            {:else if detail.error}
              <p class="err">{detail.error}</p>
            {:else if detail.documents.length === 0}
              <p class="why">Nothing has been fetched for this title yet.</p>
            {:else}
              <ul class="documents" data-testid="board-documents">
                {#each detail.documents as doc (doc.id)}
                  <li>
                    {#each documentFacts(doc) as [key, value] (key)}
                      <div class="fact">
                        <span class="data">{key}</span><span class="value">{value}</span>
                      </div>
                    {/each}
                  </li>
                {/each}
              </ul>
            {/if}
          {/if}
        </li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  .board {
    margin-top: 26px;
    padding-top: 14px;
    border-top: 1px solid var(--line);
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  h2 {
    margin: 0;
    font-size: 15px;
    font-weight: 600;
  }
  .why {
    margin: 0;
  }
  .legend {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-wrap: wrap;
    gap: 4px 12px;
  }
  .jobs,
  .documents {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .job {
    display: flex;
    flex-direction: column;
    gap: 8px;
    border-left-width: 3px;
  }
  .head {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    gap: 4px 12px;
    align-items: baseline;
  }
  .name {
    font-weight: 600;
  }
  .segments {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    grid-template-columns: repeat(10, 1fr);
    gap: 3px;
  }
  /* The progress ramp, never the accent: how far a job got is a fact about the pipeline and not a
     choice anybody made (§6.8; decision 276). Behind it at reading weight, where it stands in full
     ink, the rest the line the UI is drawn with. */
  .seg {
    height: 6px;
    border-radius: 2px;
    background: var(--progress-track);
  }
  .seg.done {
    background: var(--progress-fill);
  }
  .seg.current {
    background: var(--progress-now);
  }
  /* Decision 336's three outcomes, told apart by edge and by sentence and never by the ember:
     waiting is the bone status tone, broken is the error copy's quoted accent, stopped is the
     quietest ink, and a job moving or finished is drawn on the progress ramp. */
  .job.waiting {
    border-left-color: var(--status);
    border-left-style: dashed;
  }
  .job.broken {
    border-left-color: var(--ember-lift);
  }
  .job.stopped {
    border-left-color: var(--ink-5);
    border-left-style: dotted;
  }
  .job.moving {
    border-left-color: var(--progress-fill);
  }
  .job.finished {
    border-left-color: var(--progress-now);
  }
  .state {
    margin: 0;
  }
  .job.waiting .state {
    color: var(--status);
  }
  .job.broken .state {
    color: var(--ember-lift);
  }
  .job.stopped .state {
    color: var(--ink-5);
  }
  /* Whole and wrapped: a reason is a sentence the operator acts on, so it may run to as many lines
     as it has, and a long unbroken token (a URL, a hash) breaks rather than widening the phone. */
  .reason,
  .refusal {
    margin: 0;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    font-size: 13px;
    line-height: 1.5;
    color: var(--ink-2);
  }
  .refusal {
    color: var(--ember-lift);
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
  }
  .from {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
  .confirm {
    flex-basis: 100%;
  }
  .docs-toggle {
    align-self: flex-start;
  }
  .fact {
    display: grid;
    grid-template-columns: 70px 1fr;
    gap: 8px;
    align-items: baseline;
  }
  .value {
    font-family: var(--mono);
    font-size: 12px;
    color: var(--ink-3);
    overflow-wrap: anywhere;
  }
  .err {
    color: var(--ember-lift);
    margin: 0;
  }

  @media (pointer: coarse) {
    /* §6 preamble's 48 px floor on the narrow axis too: design.css's coarse block raises a
       `select`'s height and never its width, and a stage picker is the control a thumb reaches
       for on the phone the board is read on. */
    .from select {
      min-width: var(--touch);
    }
  }
</style>
