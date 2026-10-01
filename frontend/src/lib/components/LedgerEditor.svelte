<script>
  // One mount per ledger, each through its own routes only (decision 445). Bundle rows are
  // read-only: the next import would restore them.
  import { onMount, tick, untrack } from 'svelte';
  import { api, get, post } from '$lib/api.js';
  import {
    COMPOSER_WARNING,
    emptyForm,
    exportHref,
    ledgerOf,
    validate,
    verdictDraft,
    verdictWarning,
    visibleFields,
    withdrawPath,
    withdrawable
  } from '$lib/ledgerEditors.svelte.js';

  /** @type {{ ledger: 'adjudications' | 'corrections' }} */
  let { ledger } = $props();

  const config = $derived(ledgerOf(ledger));
  let envelope = $state(null);
  let error = $state('');
  let editing = $state(false);
  let form = $state(/** @type {Record<string, any>} */ ({}));
  let refusal = $state('');
  let saved = $state('');
  let busy = $state(false);
  // A second tap confirms Withdraw: a withdrawn verdict restores nothing it dropped.
  let confirming = $state(null);
  let root = $state(null);
  // Collapsed until opened; the review's hand-off opens it.
  let open = $state(false);
  // Starts at the count found on mount: `verdictDraft` is module state that outlives the page,
  // and a dismissed form must not reopen on the next visit.
  let handled = verdictDraft.seq;

  const rows = $derived(Array.isArray(envelope?.rows) ? envelope.rows : []);

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
    refusal = '';
    saved = '';
    editing = true;
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

  // The reject review's "Write a verdict": open prefilled and scroll to the form.
  $effect(() => {
    const seq = verdictDraft.seq;
    if (ledger !== 'adjudications' || seq <= handled) return;
    handled = seq;
    open = true;
    untrack(() => start(verdictDraft.prefill ?? {}));
    tick().then(() => root?.scrollIntoView?.({ block: 'start' }));
  });

  const ORIGIN = { household: 'Household', bundle: 'From the bundle, read-only' };

  function describe(row) {
    if (ledger === 'adjudications') {
      const on = row.scope === 'title' ? `on ${row.name ?? `title ${row.title_id}`}` : 'on every title';
      const onto = row.target ? ` onto ${row.target}` : '';
      return `${row.action} ${row.term}${onto} ${on}`;
    }
    return `${row.kind} = ${row.value} on ${row.name ?? `title ${row.title_id}`}`;
  }

  onMount(refresh);
</script>

<details class="editor" data-testid="ledger-editor" data-ledger={ledger} bind:open bind:this={root}>
  <summary class="list-row">
    <span class="text">
      <span>{config.heading}</span>
      <span class="code file">{config.artifact}</span>
    </span>
    {#if envelope}<span class="value">{rows.length || 'None'}</span>{/if}
    <svg class="chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="m5.5 9.5 6.5 6.5 6.5-6.5" /></svg>
  </summary>

  <div class="body">
    {#if envelope?.applies}<p class="footnote" data-testid="ledger-applies">{envelope.applies}</p>{/if}

    {#if error}
      <p class="err">{error}</p>
    {:else if !envelope}
      <p class="footnote">Loading…</p>
    {:else}
      {#if rows.length === 0}<p class="footnote">{config.empty}</p>{/if}
      <ul class="rows">
        {#each rows as row (row.id)}
          <li class="row" data-origin={row.origin}>
            <span class="what">{describe(row)}</span>
            <span class="footnote">{ORIGIN[row.origin] ?? row.origin}</span>
            {#if row.quote}<p class="extra">Quote: {row.quote}</p>{/if}
            {#if row.evidence}<p class="extra">Evidence: {row.evidence}</p>{/if}
            {#if row.note}<p class="extra">Note: {row.note}</p>{/if}
            {#if withdrawable(row)}
              {#if confirming === row.id && ledger === 'corrections' && row.kind === 'composer'}
                <p class="warning" data-testid="composer-warning">{COMPOSER_WARNING}</p>
              {/if}
              <div class="actions">
                {#if confirming === row.id}
                  <button class="btn-plain btn-destructive" disabled={busy} onclick={() => withdraw(row)}>
                    Yes, withdraw
                  </button>
                  <button class="btn-plain" onclick={() => (confirming = null)}>Keep it</button>
                {:else}
                  <button
                    class="btn-plain btn-destructive"
                    disabled={busy}
                    onclick={() => (confirming = row.id)}
                  >
                    Withdraw
                  </button>
                {/if}
              </div>
            {/if}
          </li>
        {/each}
      </ul>

      <div class="actions">
        {#if !editing}
          <button class="btn-plain" onclick={() => start()}>{config.add}</button>
        {/if}
        <a class="export btn-plain" href={exportHref(ledger)} download>Export household rows</a>
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
              <span class="label">{field.label}</span>
              {#if field.type === 'select'}
                <select bind:value={form[field.name]}>
                  {#if field.name === 'action'}<option value="">Choose</option>{/if}
                  {#each field.options as option (option)}
                    <option value={option}>{option}</option>
                  {/each}
                </select>
              {:else}
                <input type="text" bind:value={form[field.name]} />
              {/if}
            </label>
          {/each}
          {#if ledger === 'adjudications' && verdictWarning(form.action)}
            <p class="warning" data-testid="verdict-warning">{verdictWarning(form.action)}</p>
          {/if}
          {#if ledger === 'corrections' && form.kind === 'composer'}
            <p class="warning" data-testid="composer-warning">{COMPOSER_WARNING}</p>
          {/if}
          <div class="buttons">
            <button class="btn-primary" type="submit" disabled={busy}>{config.save}</button>
            <button class="btn-secondary" type="button" onclick={() => (editing = false)}>Cancel</button>
          </div>
        </form>
      {/if}
      {#if refusal}<p class="err refusal">{refusal}</p>{/if}
      {#if saved}<p class="footnote">{saved}</p>{/if}
    {/if}
  </div>
</details>

<style>
  summary {
    list-style: none;
    cursor: pointer;
  }
  summary::-webkit-details-marker {
    display: none;
  }
  .text {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
  }
  .file {
    color: var(--text-3);
    overflow-wrap: anywhere;
  }
  summary .value {
    font-variant-numeric: tabular-nums;
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
  .editor[open] .chevron {
    transform: rotate(180deg);
  }
  .body {
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 0 var(--gutter) var(--gutter);
  }
  .footnote,
  .err {
    margin: 0;
  }
  .rows {
    list-style: none;
    margin: 0;
    padding: 0;
    border-radius: var(--r-sm);
    background: var(--surface-2);
  }
  .row {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 10px 12px 4px;
  }
  .row + .row {
    box-shadow: inset 0 0.5px 0 var(--separator);
  }
  .rows:empty {
    display: none;
  }
  .what {
    font-size: var(--fs-subhead);
    line-height: 20px;
    overflow-wrap: anywhere;
  }
  .extra {
    margin: 0;
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-2);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    margin: 0 -8px;
  }
  .form {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .form label {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .label {
    padding: 0 4px;
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-3);
  }
  .warning {
    margin: 0;
    padding: 12px;
    border-radius: var(--r-sm);
    background: var(--warning-tint);
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text);
  }
  .buttons {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .err {
    color: var(--negative);
  }
  .refusal {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  @media (pointer: coarse) {
    /* A bare <a>: design.css's coarse floor reaches neither axis. */
    .export {
      min-height: var(--touch);
      min-width: var(--touch);
    }
  }
</style>
