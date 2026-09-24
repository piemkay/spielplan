<script>
  /**
   * §6.6 Data's review of DNA rejects and low-evidence tags. Spec v2.1 §6.6 Data, §4.1 rule 2,
   * §8 stage 7; decisions 341, 345 and 446.
   *
   * TWO ORDERINGS AND NO FILTER. The rejects arrive newest first and a title's extracted tags
   * weakest first, both from `dna/review`, and both are drawn in the order they came with every row
   * present - a tag nobody measured included, where the server put it. There is no threshold, no
   * toggle and no control of any kind on confidence, salience or n_sources: §4.1 rule 2 makes them
   * weights and never filters, and a "hide low confidence" switch here would be the cut §4.1
   * measures at 44% of the extracted tier, made in the browser instead of in SQL. The weights are
   * shown in the data voice beside the term, which is what a reviewer needs to read them.
   *
   * THE ONE ACTION WRITES A LEDGER ROW. §8 stage 7: "Failures drop, never repaired" - nothing here
   * accepts a tag back. "Write a ledger row" opens the verdict editor below with the term and the
   * title filled in, and the operator chooses what the verdict does.
   */
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

  onMount(load);
</script>

<div class="review" data-testid="dna-review">
  <h2>DNA rejects and low-evidence tags</h2>
  <p class="why">
    What the verifier dropped, newest first, and one title's extracted tags from the least evidenced
    up. Nothing is hidden and nothing here takes a tag back: a fix is a ledger row.
  </p>

  <h3 class="data">REJECTED BY THE VERIFIER</h3>
  {#if error}
    <p class="err">{error}</p>
  {:else if !rejects}
    <p class="data">loading...</p>
  {:else if rows.length === 0}
    <p class="why">The verifier has rejected nothing on this install.</p>
  {:else}
    <ul class="rows">
      {#each rows as row (row.id)}
        <li class="row card" data-testid="dna-reject">
          <div class="head">
            <span class="data-lg">{row.term}</span>
            <span class="data">{row.facet ?? ''}</span>
          </div>
          <span class="name">{titleOf(row)}</span>
          {#if row.quote != null}<p class="quote">{row.quote}</p>{/if}
          <p class="data">
            rule {row.rule_violated} - salience {figure(row.salience)} - {row.provider ?? ''} -
            {row.at ? new Date(row.at).toLocaleString() : ''}
          </p>
          <div class="actions">
            <button class="btn-ghost" onclick={() => openVerdict(ledgerPrefill(row))}>
              Write a ledger row
            </button>
            {#if row.title_id != null}
              <button class="btn-ghost" onclick={() => tagsFor(row)}>Tags for this title</button>
            {/if}
          </div>
        </li>
      {/each}
    </ul>
  {/if}

  <h3 class="data">LOW-EVIDENCE TAGS FOR ONE TITLE</h3>
  <form
    class="ask"
    onsubmit={(e) => {
      e.preventDefault();
      show();
    }}
  >
    <label>
      <span class="data">Title id</span>
      <input type="text" inputmode="numeric" bind:value={titleText} data-testid="evidence-title" />
    </label>
    <button class="btn-ghost" type="submit" disabled={asking}>Show</button>
  </form>
  {#if evidenceError}<p class="err">{evidenceError}</p>{/if}
  {#if evidence}
    {#if tags.length === 0}
      <p class="why">Title {evidence.title_id} carries no extracted tag at the active vocabulary.</p>
    {:else}
      <ol class="rows" data-testid="evidence-tags">
        {#each tags as tag, i (i)}
          <li class="row card" data-testid="evidence-tag" data-term={tag.term}>
            <div class="head">
              <span class="data-lg">{tag.term}</span>
              <span class="data">{tag.facet ?? ''} {tag.provider ?? ''}</span>
            </div>
            <p class="data">
              confidence {figure(tag.confidence)} - n_sources {figure(tag.n_sources)} - salience
              {figure(tag.salience)}
            </p>
            <div class="actions">
              <button
                class="btn-ghost"
                onclick={() => openVerdict(ledgerPrefill(tag, evidence.title_id))}
              >
                Write a ledger row
              </button>
            </div>
          </li>
        {/each}
      </ol>
    {/if}
  {/if}
</div>

<style>
  .review {
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
  h3 {
    margin: 8px 0 0;
    font-weight: 400;
    letter-spacing: 0.12em;
  }
  .why {
    margin: 0;
  }
  .rows {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .row {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .row p {
    margin: 0;
  }
  .head {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 12px;
    align-items: baseline;
  }
  .name {
    font-weight: 600;
  }
  .quote {
    font-size: 13px;
    line-height: 1.5;
    color: var(--ink-2);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .ask {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: flex-end;
  }
  .ask label {
    display: flex;
    flex-direction: column;
    gap: 6px;
    flex: 1 1 160px;
  }
  .err {
    color: var(--ember-lift);
    margin: 0;
  }
</style>
