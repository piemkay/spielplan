<script>
  // Server order with no row hidden and no confidence filter: these are weights, never filters
  // (§4.1 rule 2). Nothing here accepts a tag back; a fix is a ledger row.
  import { onMount } from 'svelte';
  import { get } from '$lib/api.js';
  import {
    REJECTS_PATH,
    evidencePath,
    evidenceRows,
    figure,
    ledgerPrefill,
    parseTitleId,
    rejectRows,
    titleOf
  } from '$lib/dnaReview.svelte.js';
  import { openVerdict } from '$lib/ledgerEditors.svelte.js';

  let rejects = $state(null);
  let error = $state('');
  let titleText = $state('');
  let evidence = $state(null);
  let evidenceError = $state('');
  let asking = $state(false);

  const rows = $derived(rejectRows(rejects));
  const tags = $derived(evidenceRows(evidence));

  async function load() {
    try {
      rejects = await get(REJECTS_PATH);
      error = '';
    } catch (err) {
      error = err.message;
    }
  }

  async function show() {
    const id = parseTitleId(titleText);
    if (id === null) {
      evidenceError = 'Type a title id: a whole number.';
      return;
    }
    asking = true;
    try {
      evidence = await get(evidencePath(id));
      evidenceError = '';
    } catch (err) {
      evidence = null;
      evidenceError = err.message;
    } finally {
      asking = false;
    }
  }

  function tagsFor(row) {
    titleText = String(row.title_id);
    show();
  }

  // Built in JS: Svelte collapses the whitespace around an {#if}, gluing the separators.
  const joined = (...parts) => parts.filter((part) => part != null && part !== '').join(', ');

  onMount(load);
</script>

<div class="review" data-testid="dna-review">
  <section class="group">
    <h2 class="list-header">Rejected tags</h2>
    <p class="footnote intro">
      What the checker dropped, newest first. Nothing here takes a tag back: a fix is a verdict.
    </p>
    {#if error}
      <p class="err">{error}</p>
    {:else if !rejects}
      <p class="footnote intro">Loading…</p>
    {:else if rows.length === 0}
      <p class="card why">The checker has rejected nothing yet.</p>
    {:else}
      <ul class="list-group rows">
        {#each rows as row (row.id)}
          <li class="row" data-testid="dna-reject">
            <span class="name">{row.term}</span>
            <span class="sub">{joined(titleOf(row), row.facet)}</span>
            {#if row.quote != null}<p class="quote">{row.quote}</p>{/if}
            {#if row.at}<p class="facts">{new Date(row.at).toLocaleString()}</p>{/if}
            <details class="tech">
              <summary>Technical details</summary>
              <p class="facts">
                {joined(`rule ${row.rule_violated}`, `salience ${figure(row.salience)}`, row.provider)}
              </p>
            </details>
            <div class="actions">
              <button class="btn-plain" onclick={() => openVerdict(ledgerPrefill(row))}>
                Write a verdict
              </button>
              {#if row.title_id != null}
                <button class="btn-plain" onclick={() => tagsFor(row)}>See this title's tags</button>
              {/if}
            </div>
          </li>
        {/each}
      </ul>
    {/if}
  </section>

  <section class="group">
    <h2 class="list-header">Weakest tags on one title</h2>
    <p class="footnote intro">One title's extracted tags, the least evidenced first.</p>
    <form
      class="ask"
      onsubmit={(e) => {
        e.preventDefault();
        show();
      }}
    >
      <label>
        <span class="sr-only">Title id</span>
        <input
          type="text"
          inputmode="numeric"
          placeholder="Title id"
          bind:value={titleText}
          data-testid="evidence-title"
        />
      </label>
      <button class="btn-secondary" type="submit" disabled={asking}>Show</button>
    </form>
    {#if evidenceError}<p class="err">{evidenceError}</p>{/if}
    {#if evidence}
      {#if tags.length === 0}
        <p class="card why">Title {evidence.title_id} carries no extracted tag yet.</p>
      {:else}
        <ol class="list-group rows" data-testid="evidence-tags">
          {#each tags as tag, i (i)}
            <li class="row" data-testid="evidence-tag" data-term={tag.term}>
              <span class="name">{tag.term}</span>
              <span class="sub">{joined(tag.facet, tag.provider)}</span>
              <p class="facts">
                {joined(
                  `confidence ${figure(tag.confidence)}`,
                  `sources ${figure(tag.n_sources)}`,
                  `salience ${figure(tag.salience)}`
                )}
              </p>
              <div class="actions">
                <button
                  class="btn-plain"
                  onclick={() => openVerdict(ledgerPrefill(tag, evidence.title_id))}
                >
                  Write a verdict
                </button>
              </div>
            </li>
          {/each}
        </ol>
      {/if}
    {/if}
  </section>
</div>

<style>
  .review {
    display: flex;
    flex-direction: column;
    gap: 32px;
  }
  .group {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .intro {
    margin: 0 0 4px;
    padding: 0 var(--gutter);
  }
  .card.why,
  .err {
    margin: 0;
  }
  .err {
    color: var(--negative);
  }
  .rows {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .row {
    position: relative;
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 12px var(--gutter) 4px;
  }
  .row + .row::before {
    content: '';
    position: absolute;
    top: 0;
    right: 0;
    left: var(--gutter);
    height: 0.5px;
    background: var(--separator);
  }
  .name {
    font-size: var(--fs-body);
    line-height: 22px;
    overflow-wrap: anywhere;
  }
  .sub,
  .facts {
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-3);
  }
  .facts {
    margin: 4px 0 0;
    font-variant-numeric: tabular-nums;
    overflow-wrap: anywhere;
  }
  .tech summary {
    width: fit-content;
    padding: 6px 0;
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-2);
    cursor: pointer;
  }
  .tech .facts {
    margin: 0 0 4px;
  }
  .quote {
    margin: 6px 0 0;
    font-size: var(--fs-subhead);
    line-height: 20px;
    color: var(--text-2);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    margin: 0 -8px;
  }
  .ask {
    display: flex;
    gap: 8px;
    align-items: stretch;
  }
  .ask label {
    flex: 1;
    min-width: 0;
  }
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
  }
</style>
