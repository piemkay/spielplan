<script>
  // Reasons print whole under Technical details: one that reads badly is fixed in its writer.
  // Document URLs are text, not links: the bytes live on the worker's volume.
  import { onMount } from 'svelte';
  import { api, get } from '$lib/api.js';
  import { noteMissing, posterSrc } from '$lib/art.js';
  import { showToast } from '$lib/toast.svelte.js';
  import Sheet from './Sheet.svelte';
  import ActionSheet from './ActionSheet.svelte';
  import {
    ABANDON,
    BOARD_LIMIT,
    CURRENT,
    DONE,
    FILTERS,
    RETRY,
    RETRY_FROM,
    WAITING,
    actionRequest,
    documentFacts,
    filterCounts,
    groupsOf,
    metaOf,
    offered,
    progressOf,
    refusalOf,
    retryStages,
    segments,
    statusOf,
    stepName,
    summaryOf
  } from '$lib/acquisitionBoard.svelte.js';

  let board = $state(null);
  let error = $state('');
  let filter = $state(WAITING);
  // The job in the sheet, re-read from each fresh board so its state and actions stay current.
  let current = $state(null);
  let refusal = $state('');
  let busy = $state(false);
  let picking = $state(false);
  let confirming = $state(false);
  let documents = $state(null);
  let showDocuments = $state(false);
  // Poster URLs that failed, so a thumb falls back to its tinted panel.
  let missing = $state({});

  const stages = $derived(board?.stages ?? []);
  const jobs = $derived(board?.jobs ?? []);
  const counts = $derived(filterCounts(jobs));
  const groups = $derived(groupsOf(jobs, filter));

  async function refresh() {
    try {
      board = await get('/admin/acquisition');
      error = '';
      if (current) current = board.jobs.find((job) => job.title_id === current.title_id) ?? current;
    } catch (err) {
      error = err.message;
    }
  }

  function open(job) {
    current = job;
    refusal = '';
    documents = null;
    showDocuments = false;
  }

  const DONE_SAYS = {
    [RETRY]: (job) => `Retrying ${job.name}`,
    [RETRY_FROM]: (job, stage) =>
      `${job.name} runs again from ${stepName(stages.find((s) => s.number === stage))}`,
    [ABANDON]: (job) => `Stopped trying ${job.name}`
  };

  async function act(action, stage = undefined) {
    const job = current;
    const { path, body } = actionRequest(action, job.title_id, stage);
    busy = true;
    try {
      await api(path, { method: 'POST', body });
      refusal = '';
      showToast(DONE_SAYS[action](job, stage));
    } catch (err) {
      refusal = refusalOf(err);
    } finally {
      busy = false;
    }
    await refresh();
  }

  async function toggleDocuments() {
    showDocuments = !showDocuments;
    if (!showDocuments || documents) return;
    try {
      documents = (await get(`/admin/acquisition/${current.title_id}`)).documents;
    } catch (err) {
      documents = { error: err.message };
    }
  }

  onMount(refresh);
</script>

{#snippet thumb(job, size)}
  {@const src = posterSrc(job)}
  <span class="poster thumb {size}" aria-hidden="true">
    {#if src && !missing[src]}
      <img
        {src}
        alt=""
        loading="lazy"
        decoding="async"
        onerror={() => {
          missing[src] = true;
          noteMissing(src);
        }}
      />
    {/if}
  </span>
{/snippet}

{#snippet icon(state, tone)}
  {#if state === DONE}
    <svg class="step-icon done" viewBox="0 0 24 24" role="img" aria-label="Done">
      <circle cx="12" cy="12" r="8.5" /><path d="m8.5 12.2 2.4 2.4 4.6-4.9" />
    </svg>
  {:else if state === CURRENT && tone === 'broken'}
    <svg class="step-icon broken" viewBox="0 0 24 24" role="img" aria-label="Failed">
      <path d="M12 4 2.8 19.5h18.4z" /><path d="M12 10v4.5M12 17.2v.01" />
    </svg>
  {:else if state === CURRENT && tone === 'stopped'}
    <svg class="step-icon stopped" viewBox="0 0 24 24" role="img" aria-label="Stopped">
      <circle cx="12" cy="12" r="8.5" /><path d="M8.5 12h7" />
    </svg>
  {:else if state === CURRENT}
    <svg class="step-icon {tone}" viewBox="0 0 24 24" role="img" aria-label="Here now">
      <circle cx="12" cy="12" r="8.5" /><path d="M12 7.5V12l3 2" />
    </svg>
  {:else}
    <svg class="step-icon unreached" viewBox="0 0 24 24" role="img" aria-label="Not started">
      <circle cx="12" cy="12" r="8.5" />
    </svg>
  {/if}
{/snippet}

<div class="board" data-testid="acquisition-board">
  <div class="filter">
    <div class="segmented" role="group" aria-label="Show">
      {#each FILTERS as f (f.key)}
        <button aria-pressed={filter === f.key} onclick={() => (filter = f.key)}>
          {f.label} · {counts[f.key]}
        </button>
      {/each}
    </div>
    <p class="footnote">New titles from Jellyfin pass ten steps before they reach your shelves.</p>
    {#if jobs.length >= BOARD_LIMIT}
      <p class="footnote">
        Showing the {BOARD_LIMIT} most recently moved titles; the counts cover only these.
      </p>
    {/if}
  </div>

  {#if error}
    <p class="err">{error}</p>
  {:else if !board}
    <p class="footnote">Loading…</p>
  {:else if groups.length === 0}
    <p class="empty why">
      {#if filter === WAITING}
        Nothing is waiting. New titles are finding their own way to your shelves.
      {:else if jobs.length === 0}
        No new titles yet. When Jellyfin adds one, it shows up here.
      {:else}
        No title has finished yet.
      {/if}
    </p>
  {:else}
    {#each groups as group (group.tone)}
      <section class="group">
        <h2 class="list-header">{group.heading}</h2>
        <div class="list-group">
          {#each group.jobs as job (job.title_id)}
            {@const progress = progressOf(stages, job)}
            <button
              class="row {group.tone}"
              data-testid="board-job"
              data-title-id={job.title_id}
              data-status={job.status}
              onclick={() => open(job)}
            >
              {@render thumb(job, 'small')}
              <span class="text">
                <span class="name">{job.name}</span>
                {#if metaOf(job)}<span class="sub">{metaOf(job)}</span>{/if}
              </span>
              <span class="progress">
                <span class="count">{progress.done} of {progress.total}</span>
                <span class="sub">steps</span>
              </span>
              <svg class="chevron" viewBox="0 0 24 24" aria-hidden="true">
                <path d="m9.5 5.5 6.5 6.5-6.5 6.5" />
              </svg>
            </button>
          {/each}
        </div>
      </section>
    {/each}
  {/if}
</div>

<Sheet open={current !== null} onClose={() => (current = null)} label={current?.name ?? 'Title'}>
  {#snippet children(close)}
    <div class="bar">
      <button class="btn-plain done-btn" onclick={close}>Done</button>
    </div>
    {#if current}
      {@const status = statusOf(current, stages)}
      {@const can = offered(current)}
      <div
        class="detail {status.tone}"
        data-testid="board-title"
        data-title-id={current.title_id}
        data-status={current.status}
      >
        <div class="head">
          {@render thumb(current, 'large')}
          <div class="head-text">
            <h2 class="title-1">{current.name}</h2>
            {#if metaOf(current)}<p class="meta">{metaOf(current)}</p>{/if}
            <span class="badge {status.badge}" data-testid="board-status">
              <span class="dot" aria-hidden="true"></span>{status.label}
            </span>
          </div>
        </div>

        <div class="card summary">
          <p>{summaryOf(stages, current)}</p>
          {#if current.retry_after && current.status === 'parked'}
            <p class="footnote">Tries again {new Date(current.retry_after).toLocaleString()}.</p>
          {/if}
        </div>

        <section class="group">
          <h3 class="list-header">Progress</h3>
          <ol class="list-group steps" data-testid="board-stages">
            {#each segments(stages, current) as seg (seg.number)}
              <li class="step {seg.state}">
                {@render icon(seg.state, status.tone)}
                <span class="text">
                  <span class="name">{seg.plain}</span>
                  <span class="sub">
                    <span data-testid="board-stage">{seg.name}</span>{#if seg.state === CURRENT}
                      <span class="now">{` · ${status.label.toLowerCase()}`}</span>
                    {/if}
                  </span>
                </span>
              </li>
            {/each}
          </ol>
        </section>

        {#if can.retry || can.retryFrom || can.abandon}
          <section class="group">
            <h3 class="list-header">Actions</h3>
            <div class="list-group actions">
              {#if can.retry}
                <button class="action" disabled={busy} onclick={() => act(RETRY)}>
                  <svg viewBox="0 0 24 24" aria-hidden="true">
                    <path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3" /><path d="M19.5 4.5v4h-4" />
                  </svg>
                  <span>Retry now</span>
                </button>
              {/if}
              {#if can.retryFrom}
                <button class="action" disabled={busy} onclick={() => (picking = true)}>
                  <svg viewBox="0 0 24 24" aria-hidden="true">
                    <path d="M9 14 4 9l5-5" /><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11" />
                  </svg>
                  <span>Retry from a step…</span>
                  <svg class="chevron" viewBox="0 0 24 24" aria-hidden="true">
                    <path d="m9.5 5.5 6.5 6.5-6.5 6.5" />
                  </svg>
                </button>
              {/if}
              {#if can.abandon}
                <button class="action destructive" disabled={busy} onclick={() => (confirming = true)}>
                  <svg viewBox="0 0 24 24" aria-hidden="true">
                    <path d="M6.5 6.5l11 11M17.5 6.5l-11 11" />
                  </svg>
                  <span>Stop trying</span>
                </button>
              {/if}
            </div>
            {#if refusal}
              <p class="refusal" data-testid="board-refusal">{refusal}</p>
            {/if}
          </section>
        {/if}

        <details class="group tech">
          <summary class="list-header">
            <span>Technical details</span>
            <svg class="chevron down" viewBox="0 0 24 24" aria-hidden="true">
              <path d="m5.5 9.5 6.5 6.5 6.5-6.5" />
            </svg>
          </summary>
          <div class="tech-body">
            {#if current.reason != null}
              <pre class="code" data-testid="board-reason">{current.reason}</pre>
            {/if}
            <p class="code facts">
              title {current.title_id} · {current.status} at stage {current.stage}
            </p>
            <div class="list-group">
              <button
                class="action plain"
                aria-expanded={showDocuments ? 'true' : 'false'}
                onclick={toggleDocuments}
              >
                <span>Fetched documents</span>
                <svg class="chevron" class:turned={showDocuments} viewBox="0 0 24 24" aria-hidden="true">
                  <path d="m9.5 5.5 6.5 6.5-6.5 6.5" />
                </svg>
              </button>
            </div>
            {#if showDocuments}
              {#if !documents}
                <p class="footnote">Loading…</p>
              {:else if documents.error}
                <p class="err">{documents.error}</p>
              {:else if documents.length === 0}
                <p class="footnote">Nothing has been fetched for this title yet.</p>
              {:else}
                <ul class="documents" data-testid="board-documents">
                  {#each documents as doc (doc.id)}
                    <li class="card">
                      {#each documentFacts(doc) as [key, value] (key)}
                        <div class="fact">
                          <span class="footnote">{key}</span><span class="code">{value}</span>
                        </div>
                      {/each}
                    </li>
                  {/each}
                </ul>
              {/if}
            {/if}
          </div>
        </details>
      </div>
    {/if}
  {/snippet}
</Sheet>

<ActionSheet
  open={picking}
  title="Retry from which step? Nothing before it runs again."
  options={current
    ? retryStages(stages, current).map((stage) => ({
        label: stepName(stage),
        checked: stage.number === Number(current.stage),
        onSelect: () => act(RETRY_FROM, stage.number)
      }))
    : []}
  onClose={() => (picking = false)}
/>

<ActionSheet
  open={confirming}
  title="Nothing runs for this title until it's retried from a step."
  options={[{ label: 'Stop trying', destructive: true, onSelect: () => act(ABANDON) }]}
  onClose={() => (confirming = false)}
/>

<style>
  .board {
    display: flex;
    flex-direction: column;
    gap: 32px;
  }
  .filter {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .filter .footnote {
    margin: 0;
    padding: 0 4px;
  }
  .group {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .empty,
  .err {
    margin: 0;
  }
  .err,
  .refusal {
    color: var(--negative);
  }

  .row,
  .step,
  .action {
    position: relative;
    display: flex;
    align-items: center;
    gap: 12px;
    width: 100%;
    border: none;
    background: none;
    color: var(--text);
    text-align: left;
  }
  .row {
    min-height: 76px;
    padding: 8px 12px 8px 16px;
  }
  .row + .row::before,
  .step + .step::before,
  .action + .action::before {
    content: '';
    position: absolute;
    top: 0;
    right: 0;
    left: 68px;
    height: 0.5px;
    background: var(--separator);
  }
  .step + .step::before,
  .action + .action::before {
    left: 52px;
  }
  .text {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
  }
  .name {
    font-size: var(--fs-body);
    line-height: 22px;
    overflow-wrap: anywhere;
  }
  .sub {
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-3);
    font-variant-numeric: tabular-nums;
  }
  .progress {
    flex: none;
    display: flex;
    flex-direction: column;
    align-items: flex-end;
  }
  .count {
    font-size: var(--fs-subhead);
    line-height: 20px;
    color: var(--text-2);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }
  .chevron {
    flex: none;
    width: 16px;
    height: 16px;
    fill: none;
    stroke: rgba(245, 240, 232, 0.35);
    stroke-width: 1.75;
    stroke-linecap: round;
    stroke-linejoin: round;
    transition: transform 0.2s var(--ease);
  }
  .chevron.turned {
    transform: rotate(90deg);
  }

  .thumb {
    flex: none;
    display: block;
    background: var(--surface-2);
  }
  .thumb.small {
    width: 40px;
    height: 60px;
  }
  .thumb.large {
    width: 80px;
    height: 120px;
  }
  .thumb img {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    object-fit: cover;
  }

  .bar {
    position: sticky;
    top: 0;
    z-index: 1;
    display: flex;
    justify-content: flex-end;
    margin: 0 -8px;
    background: var(--bg-elevated);
  }
  .done-btn {
    font-weight: 600;
  }
  .detail {
    display: flex;
    flex-direction: column;
    gap: 32px;
    padding-top: 8px;
  }
  .head {
    display: flex;
    align-items: center;
    gap: 16px;
  }
  .head-text {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 4px;
  }
  .meta {
    margin: 0 0 8px;
    font-size: var(--fs-subhead);
    line-height: 20px;
    color: var(--text-2);
    font-variant-numeric: tabular-nums;
  }
  .badge .dot {
    width: 6px;
    height: 6px;
    border-radius: var(--r-pill);
    background: currentColor;
  }
  .summary {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .summary p {
    margin: 0;
    font-size: var(--fs-callout);
    line-height: 21px;
    color: var(--text-2);
    text-wrap: pretty;
  }
  .summary .footnote {
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-3);
  }

  .steps {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .step {
    min-height: 52px;
    padding: 6px 12px 6px 16px;
  }
  .step.current .name {
    font-weight: 600;
  }
  .step-icon {
    flex: none;
    width: 24px;
    height: 24px;
    fill: none;
    stroke: currentColor;
    stroke-width: 1.75;
    stroke-linecap: round;
    stroke-linejoin: round;
    color: var(--text-3);
  }
  .step-icon.done {
    color: var(--positive);
  }
  .step-icon.waiting {
    color: var(--warning);
  }
  .step-icon.broken {
    color: var(--negative);
  }
  .step-icon.moving,
  .step-icon.finished {
    color: var(--text);
  }
  .detail.waiting .step.current .now {
    color: var(--warning);
  }
  .detail.broken .step.current .now {
    color: var(--negative);
  }

  .action {
    min-height: 52px;
    padding: 0 12px 0 16px;
    color: var(--accent-text);
    font-size: var(--fs-body);
    line-height: 22px;
  }
  .action > span {
    flex: 1;
    min-width: 0;
  }
  .action > svg:not(.chevron) {
    flex: none;
    width: 24px;
    height: 24px;
    fill: none;
    stroke: currentColor;
    stroke-width: 1.75;
    stroke-linecap: round;
    stroke-linejoin: round;
  }
  .action.destructive {
    color: var(--negative);
  }
  .action.plain {
    color: var(--text);
  }
  .action:disabled {
    opacity: 0.45;
  }
  .refusal {
    margin: 0;
    padding: 0 var(--gutter);
    font-size: var(--fs-footnote);
    line-height: 18px;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  .tech summary {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    min-height: 44px;
    padding: 0 12px 0 var(--gutter);
    list-style: none;
    cursor: pointer;
  }
  .tech summary::-webkit-details-marker {
    display: none;
  }
  .tech summary .chevron.down {
    stroke: currentColor;
  }
  .tech[open] summary .chevron.down {
    transform: rotate(180deg);
  }
  .tech-body {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  pre.code {
    margin: 0;
    padding: 12px;
    border-radius: var(--r-sm);
    background: var(--surface-1);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .facts {
    margin: 0;
    padding: 0 4px;
    color: var(--text-3);
  }
  .documents {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .documents .card {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .fact {
    display: grid;
    grid-template-columns: 64px minmax(0, 1fr);
    gap: 8px;
    align-items: baseline;
  }
  .fact .code {
    overflow-wrap: anywhere;
  }
</style>
