<script>
  /**
   * One of §6.6 Data's three ledger editors, mounted once per ledger. Spec v2.1 §6.6 Data, §8
   * stages 3 and 7, §6.4; decisions 173, 326, 342 and 445.
   *
   * THREE MOUNTS, THREE ARTIFACTS, NO MERGED SCREEN. Proposal 105 (provenance for §6.6 Data under
   * decision 445): "three separate editors with separate semantics". The page mounts this once for
   * the DNA verdicts, once for the credit facts and once for the per-facet axes, and each mount
   * reads, writes, withdraws and exports through its own ledger's routes only - `ledger` picks the
   * entry in `$lib/ledgerEditors.svelte.js` and there is no path from one mount's form to another's.
   *
   * EACH SHOWS ITS ROWS, THEIR PROVENANCE AND WHAT RE-APPLIES THEM (proposal 105's own clause). The
   * rows carry `origin`: a bundle row is read-only here, because the next import would restore it,
   * and a household row takes effect beside it (decision 423) and survives every re-derive and
   * re-import (decision 326). `applies` is the server's sentence about where the ledger is read,
   * and for the axes it is also the editor's standing notice - no axis file has been authored and the
   * bundle ships none (decision 173), and a saved axis turns on Tonight's split now and the Map at M6
   * (decision 342) - printed as the server words it, so the two cannot come to say different things.
   * Withdraw is offered on household rows alone, and Export downloads the household's rows as the
   * importer's own file, so a fix made here can travel back into the corpus unchanged.
   *
   * AN AXIS IS CHANGED FROM ITS OWN ROW, NEVER BY ADDING OVER IT. A save writes the facet's whole
   * axis, so Add offers only a facet with none, and Edit opens a household axis with every term it
   * has, under a Save that says it replaces. [M5.6 review cycle 1, m56-curated-02]
   */
  import { onMount, tick, untrack } from 'svelte';
  import { api, get, post } from '$lib/api.js';
  import {
    AXIS_REPLACE_NOTE,
    COMPOSER_WARNING,
    axisForm,
    emptyForm,
    exportHref,
    facetChoices,
    ledgerOf,
    rowKey,
    rowsOf,
    validate,
    verdictDraft,
    verdictWarning,
    visibleFields,
    withdrawPath,
    withdrawable
  } from '$lib/ledgerEditors.svelte.js';

  /** @type {{ ledger: 'adjudications' | 'corrections' | 'axes' }} */
  let { ledger } = $props();

  const config = $derived(ledgerOf(ledger));
  let envelope = $state(null);
  let error = $state('');
  let editing = $state(false);
  let form = $state(/** @type {Record<string, any>} */ ({}));
  let refusal = $state('');
  let saved = $state('');
  let busy = $state(false);
  // The household row a Withdraw is waiting on a second tap for. A withdrawn row is the household's
  // own fix gone, and a withdrawn verdict restores nothing it dropped (decision 445), so one stray
  // tap on a phone must not be enough.
  let confirming = $state(null);
  // The facet whose household axis the open form is editing, or null while it adds.
  let replacing = $state(null);
  let root = $state(null);
  // The hand-off this mount has already answered. It starts at the count the mount found, because
  // `verdictDraft` is module state and outlives the page: a verdict form the operator dismissed must
  // not reopen, and scroll the page to itself, every time /admin/data is visited again in the
  // session. [M5.6 review cycle 1, M56-DATA-06]
  let handled = verdictDraft.seq;

  const rows = $derived(rowsOf(ledger, envelope));
  const facets = $derived(envelope?.facets ?? []);

  async function refresh() {
    try {
      envelope = await get(config.path);
      error = '';
    } catch (err) {
      error = err.message;
    }
  }

  function start(prefill = {}) {
    form = { ...emptyForm(ledger), ...prefill };
    replacing = null;
    refusal = '';
    saved = '';
    editing = true;
  }

  function edit(row) {
    start(axisForm(row));
    replacing = row.facet;
  }

  async function save() {
    const checked = validate(ledger, form);
    if (!checked.ok) {
      refusal = checked.reason;
      return;
    }
    busy = true;
    try {
      await post(config.path, checked.body);
      editing = false;
      refusal = '';
      saved = 'Saved.';
    } catch (err) {
      refusal = err.message;
    } finally {
      busy = false;
    }
    await refresh();
  }

  async function withdraw(row) {
    confirming = null;
    busy = true;
    try {
      await api(withdrawPath(ledger, row), { method: 'DELETE' });
      refusal = '';
      saved = 'Withdrawn.';
    } catch (err) {
      refusal = err.message;
    } finally {
      busy = false;
    }
    await refresh();
  }

  // The reject review's "Write a ledger row" (decision 445): the verdict editor opens with the
  // review row's term and title and is scrolled to, so the tap lands on the form it opened.
  $effect(() => {
    const seq = verdictDraft.seq;
    if (ledger !== 'adjudications' || seq <= handled) return;
    handled = seq;
    untrack(() => start(verdictDraft.prefill ?? {}));
    tick().then(() => root?.scrollIntoView?.({ block: 'start' }));
  });

  function describe(row) {
    if (ledger === 'adjudications') {
      const on = row.scope === 'title' ? `on ${row.name ?? `title ${row.title_id}`}` : 'on every title';
      const onto = row.target ? ` onto ${row.target}` : '';
      return `${row.action} ${row.term}${onto} ${on}`;
    }
    if (ledger === 'corrections') {
      return `${row.kind} = ${row.value} on ${row.name ?? `title ${row.title_id}`}`;
    }
    return `${row.facet}: ${row.left_pole} to ${row.right_pole}`;
  }

  onMount(refresh);
</script>

<div class="editor" data-testid="ledger-editor" data-ledger={ledger} bind:this={root}>
  <h3>{config.heading}</h3>
  <p class="data">{config.artifact}</p>
  {#if envelope?.applies}<p class="why" data-testid="ledger-applies">{envelope.applies}</p>{/if}

  {#if error}
    <p class="err">{error}</p>
  {:else if !envelope}
    <p class="data">loading...</p>
  {:else}
    {#if rows.length === 0}<p class="why">{config.empty}</p>{/if}
    <ul class="rows">
      {#each rows as row (rowKey(ledger, row))}
        <li class="row" data-origin={row.origin}>
          <div class="head">
            <span class="what">{describe(row)}</span>
            <span class="data">{row.origin}</span>
          </div>
          {#if row.quote}<p class="extra">quote: {row.quote}</p>{/if}
          {#if row.evidence}<p class="extra">evidence: {row.evidence}</p>{/if}
          {#if ledger === 'axes'}
            <p class="data">
              {#each row.weights ?? [] as w (w.term)}<span class="weight">{w.term} {w.weight}</span>{/each}
            </p>
          {/if}
          {#if row.note}<p class="extra">note: {row.note}</p>{/if}
          {#if withdrawable(row)}
            {#if confirming === rowKey(ledger, row) && ledger === 'corrections' && row.kind === 'composer'}
              <p class="why" data-testid="composer-warning">{COMPOSER_WARNING}</p>
            {/if}
            <div class="actions">
              {#if confirming === rowKey(ledger, row)}
                <button class="btn-ghost" disabled={busy} onclick={() => withdraw(row)}>
                  Yes, withdraw
                </button>
                <button class="btn-ghost" onclick={() => (confirming = null)}>Keep it</button>
              {:else}
                <button
                  class="btn-ghost"
                  disabled={busy}
                  onclick={() => (confirming = rowKey(ledger, row))}
                >
                  Withdraw
                </button>
                {#if ledger === 'axes'}
                  <button class="btn-ghost" disabled={busy} onclick={() => edit(row)}>Edit</button>
                {/if}
              {/if}
              {#if ledger === 'axes'}
                <a class="export" href={exportHref(ledger, row)} download>Export {row.facet}.tsv</a>
              {/if}
            </div>
          {/if}
        </li>
      {/each}
    </ul>

    <div class="actions">
      {#if ledger !== 'axes'}
        <a class="export" href={exportHref(ledger)} download>Export household rows</a>
      {/if}
      {#if !editing}
        <button class="btn-ghost" onclick={() => start()}>{config.add}</button>
      {/if}
    </div>

    {#if editing}
      <form
        class="form"
        onsubmit={(e) => {
          e.preventDefault();
          save();
        }}
      >
        {#each visibleFields(ledger, form) as field (field.name)}
          <label>
            <span class="data">{field.label}</span>
            {#if field.type === 'select'}
              <select bind:value={form[field.name]}>
                {#if field.name === 'action'}<option value="">choose</option>{/if}
                {#each field.options as option (option)}
                  <option value={option}>{option}</option>
                {/each}
              </select>
            {:else if field.type === 'facet'}
              <select bind:value={form[field.name]}>
                <option value="">choose</option>
                {#each facetChoices(facets, rows, replacing) as facet (facet)}
                  <option value={facet}>{facet}</option>
                {/each}
              </select>
            {:else if field.type === 'textarea'}
              <textarea rows="5" bind:value={form[field.name]}></textarea>
            {:else}
              <input type="text" bind:value={form[field.name]} />
            {/if}
          </label>
        {/each}
        {#if ledger === 'adjudications' && verdictWarning(form.action)}
          <p class="why" data-testid="verdict-warning">{verdictWarning(form.action)}</p>
        {/if}
        {#if ledger === 'axes' && replacing}
          <p class="why" data-testid="axis-replace-note">{AXIS_REPLACE_NOTE}</p>
        {/if}
        {#if ledger === 'corrections' && form.kind === 'composer'}
          <p class="why" data-testid="composer-warning">{COMPOSER_WARNING}</p>
        {/if}
        <div class="actions">
          <button class="btn-ghost" type="submit" disabled={busy}>
            {replacing ? config.replace : config.save}
          </button>
          <button class="btn-ghost" type="button" onclick={() => (editing = false)}>Cancel</button>
        </div>
      </form>
    {/if}
    {#if refusal}<p class="err refusal">{refusal}</p>{/if}
    {#if saved}<p class="why">{saved}</p>{/if}
  {/if}
</div>

<style>
  .editor {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding-top: 12px;
    border-top: 1px solid var(--line);
  }
  h3 {
    margin: 0;
    font-size: 14px;
    font-weight: 600;
  }
  .why,
  .data {
    margin: 0;
  }
  .rows {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .row {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 8px 0;
    border-bottom: 1px solid var(--line);
  }
  .head {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    gap: 4px 12px;
    align-items: baseline;
  }
  .what {
    font-family: var(--mono);
    font-size: 12px;
    color: var(--ink-2);
    overflow-wrap: anywhere;
  }
  .extra {
    margin: 0;
    font-size: 13px;
    line-height: 1.5;
    color: var(--ink-3);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .weight {
    margin-right: 10px;
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
  }
  /* A download, not an accent: underlined in the quiet ink, for `DataSources.svelte`'s reason -
     §6.8 spends the ember on selection and primary actions, and a file an operator may save is
     neither (decision 276). */
  .export {
    display: inline-flex;
    align-items: center;
    color: var(--ink-3);
    text-decoration: underline;
  }
  .form {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .form label {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  textarea {
    width: 100%;
    padding: 11px 13px;
    border-radius: var(--r-sm);
    border: 1px solid var(--line-2);
    background: var(--card);
    color: var(--ink);
    font-family: var(--mono);
  }
  .err {
    color: var(--ember-lift);
    margin: 0;
  }
  .refusal {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  @media (pointer: coarse) {
    /* §6 preamble's 48 px floor, both axes: an export is a bare `<a>`, which design.css's coarse
       block does not reach on either, and it sits beside 48 px buttons a thumb is already aimed at. */
    .export {
      min-height: var(--touch);
      min-width: var(--touch);
    }
  }
</style>
