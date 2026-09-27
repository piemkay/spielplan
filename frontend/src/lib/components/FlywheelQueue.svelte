<script>
  // Launch is held against the batch reservation the server re-quotes on every change, and the
  // launch route prices the batch again whatever this card shows (decision 441).
  import { onDestroy, onMount } from 'svelte';
  import { get, post } from '$lib/api.js';
  import { statusOf, stepName } from '$lib/acquisitionBoard.svelte.js';
  import { PROVIDER_LABELS } from '$lib/spendGuard.svelte.js';
  import {
    acceptQuote,
    defaultPlan,
    dollars,
    kindLabel,
    launchBody,
    launchState,
    orderedProviders,
    passChoices,
    queuedMarker,
    quoteKey,
    quotePath,
    roomLeft,
    titlesOf,
    toggle
  } from '$lib/flywheel.svelte.js';

  // Ticking five rows asks once, and the figure still follows the finger.
  const QUOTE_DEBOUNCE_MS = 200;
  // Half the marker's finest unit (a minute), so "just now" never long outlives its minute.
  const CLOCK_MS = 30_000;

  let envelope = $state(null);
  let error = $state('');
  let selected = $state(new Set());
  let chosen = $state([]);
  let passes = $state(1);
  let quoted = $state(null);
  let quoteError = $state('');
  let busy = $state(false);
  let launchRefusal = $state('');
  let launched = $state('');
  let now = $state(Date.now());
  let timer = null;
  let clock = null;

  const items = $derived(envelope?.items ?? []);
  const providers = $derived(envelope?.providers ?? []);
  const stages = $derived(envelope?.stages ?? []);
  const current = $derived({
    titles: titlesOf(items, selected),
    providers: orderedProviders(providers, chosen),
    passes
  });
  const currentKey = $derived(quoteKey(current));
  const launch = $derived(launchState(quoted, currentKey, busy));
  const figures = $derived(quoted && quoted.key === currentKey ? quoted.body : null);

  async function refresh() {
    try {
      const fresh = await get('/admin/flywheel');
      if (!envelope) {
        const plan = defaultPlan(fresh);
        chosen = plan.providers;
        passes = plan.passes;
      }
      envelope = fresh;
      now = Date.now();
      // A row that closed or launched since it was ticked leaves the selection.
      const live = new Set((fresh.items ?? []).map((item) => item.id));
      selected = new Set([...selected].filter((id) => live.has(id)));
      error = '';
    } catch (err) {
      error = err.message;
    }
  }

  async function ask(requested) {
    try {
      const body = await get(quotePath(requested));
      const kept = acceptQuote(current, requested, body);
      if (kept) {
        quoted = kept;
        quoteError = '';
      }
    } catch (err) {
      if (quoteKey(requested) === quoteKey(current)) quoteError = err.message;
    }
  }

  // The request carries a snapshot, so a late answer is matched against what is on screen.
  $effect(() => {
    if (!envelope) return;
    const requested = {
      titles: current.titles,
      providers: [...current.providers],
      passes: current.passes
    };
    clearTimeout(timer);
    timer = setTimeout(() => ask(requested), QUOTE_DEBOUNCE_MS);
  });

  function pick(id) {
    selected = toggle(selected, id);
    launched = '';
  }

  function chooseProvider(name, on) {
    chosen = on ? [...chosen.filter((p) => p !== name), name] : chosen.filter((p) => p !== name);
  }

  async function doLaunch() {
    busy = true;
    launchRefusal = '';
    launched = '';
    try {
      const answer = await post('/admin/flywheel/launch', launchBody(selected, current));
      const n = answer.items.length;
      launched = `Launched: ${n} title${n === 1 ? ' is' : 's are'} being read now.`;
      selected = new Set();
    } catch (err) {
      launchRefusal = err.message;
    } finally {
      busy = false;
    }
    await refresh();
  }

  function titleOf(item) {
    if (item.title) return item.title.year ? `${item.title.name} (${item.title.year})` : item.title.name;
    return item.title_id == null ? '' : `title ${item.title_id}`;
  }

  onMount(() => {
    clock = setInterval(() => (now = Date.now()), CLOCK_MS);
    refresh();
  });
  onDestroy(() => {
    clearTimeout(timer);
    clearInterval(clock);
  });
</script>

<section class="flywheel" data-testid="flywheel-queue" aria-labelledby="queue-title">
  <h2 class="list-header" id="queue-title">Extraction queue</h2>

  {#if error}
    <p class="err" role="alert">{error}</p>
  {:else if !envelope}
    <p class="footnote">Loading…</p>
  {:else}
    {#if items.length === 0}
      <div class="list-group"><p class="list-row empty">Nothing is waiting.</p></div>
    {:else}
      <ul class="list-group items">
        {#each items as item (item.id)}
          <li class="item" class:picked={selected.has(item.id)} data-testid="flywheel-row">
            <label class="pick">
              <input
                type="checkbox"
                checked={selected.has(item.id)}
                onchange={() => pick(item.id)}
                aria-label="Select row {item.id}"
              />
            </label>
            <div class="body">
              {#if titleOf(item)}<span class="name">{titleOf(item)}</span>{/if}
              <span class="meta">
                {kindLabel(item.kind)} ·
                {#if item.status === 'queued' && queuedMarker(item.created_at, now)}
                  <span data-testid="flywheel-queued">{queuedMarker(item.created_at, now)}</span>
                {:else}
                  {item.status}
                {/if}
              </span>
              <p class="reason" data-testid="flywheel-reason">{item.reason}</p>
              {#if item.status === 'running' && item.board}
                {@const step = stepName(stages.find((s) => s.number === Number(item.board.stage)))}
                <p class="meta" data-testid="flywheel-board">
                  {statusOf(item.board, stages).label}{#if step}{' · '}{step}{/if}
                </p>
                <details class="tech">
                  <summary>Technical details</summary>
                  <div class="code lines">
                    <p>stage {item.board.stage} · {item.board.status}</p>
                    {#if item.board.reason != null}<p>{item.board.reason}</p>{/if}
                  </div>
                </details>
              {/if}
            </div>
          </li>
        {/each}
      </ul>
    {/if}
    <p class="list-footer">
      Titles whose details came back thin, each with the reason. Pick some, choose who reads them and
      how often, and launch a batch within what is left of the monthly cap.
    </p>

    <h3 class="list-header">This batch</h3>
    <div class="list-group">
      {#each providers as provider (provider.name)}
        <label class="list-row provider">
          <span class="grow">
            <span>{PROVIDER_LABELS[provider.name] ?? provider.name}</span>
            {#if provider.reason}<span class="note">{provider.reason}</span>{/if}
          </span>
          <input
            type="checkbox"
            checked={chosen.includes(provider.name)}
            disabled={!provider.configured}
            onchange={(e) => chooseProvider(provider.name, e.currentTarget.checked)}
          />
        </label>
      {/each}
      <label class="list-row passes">
        <span class="grow">Passes</span>
        <select value={passes} onchange={(e) => (passes = Number(e.currentTarget.value))}>
          {#each passChoices(envelope.defaults?.passes) as n (n)}
            <option value={n}>{n}</option>
          {/each}
        </select>
      </label>
    </div>

    <dl class="list-group figures" data-testid="flywheel-figures">
      <div class="list-row">
        <dt>Titles selected</dt>
        <dd data-testid="flywheel-titles">{current.titles}</dd>
      </div>
      <div class="list-row"><dt>Per title</dt><dd>{dollars(figures?.per_title_usd)}</dd></div>
      <div class="list-row"><dt>Batch total</dt><dd>{dollars(figures?.total_usd)}</dd></div>
      <div class="list-row">
        <dt>Held for both attempts</dt>
        <dd data-testid="flywheel-reserved">{dollars(figures?.reserved_usd)}</dd>
      </div>
      <div class="list-row">
        <dt>Left this month</dt>
        <dd>{roomLeft(figures, envelope.meter)}</dd>
      </div>
    </dl>

    <div class="launch">
      <button
        class="btn-secondary"
        data-testid="flywheel-launch"
        disabled={launch.disabled}
        onclick={doLaunch}
      >
        Launch
      </button>
      {#if launch.reason}
        <p class="reason" data-testid="flywheel-launch-reason">{launch.reason}</p>
      {/if}
    </div>
    {#if quoteError}<p class="err" role="alert">{quoteError}</p>{/if}
    {#if launchRefusal}<p class="err refusal" data-testid="flywheel-refusal">{launchRefusal}</p>{/if}
    {#if launched}<p class="why">{launched}</p>{/if}
  {/if}
</section>

<style>
  .flywheel {
    display: flex;
    flex-direction: column;
  }
  .items {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .item {
    display: flex;
    gap: 4px;
    align-items: flex-start;
    padding: 4px var(--gutter) 12px 4px;
  }
  .item + .item {
    box-shadow: inset 0 0.5px 0 var(--separator);
  }
  /* Selection is a neutral fill, not the accent (decision 527). */
  .item.picked {
    background: var(--surface-2);
  }
  .pick {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex: none;
    min-width: var(--touch);
    min-height: var(--touch);
    cursor: pointer;
  }
  .pick input,
  .provider input {
    width: 20px;
    height: 20px;
    margin: 0;
  }
  .body {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
    padding-top: 12px;
  }
  .name {
    font-size: var(--fs-body);
    line-height: 22px;
  }
  .meta,
  .note {
    margin: 0;
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-3);
  }
  .meta {
    display: block;
  }
  .meta::first-letter {
    text-transform: uppercase;
  }
  .note {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .reason {
    margin: 0;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    font-size: var(--fs-subhead);
    line-height: 20px;
    color: var(--text-2);
  }
  .empty {
    margin: 0;
    color: var(--text-3);
  }
  .tech summary {
    display: flex;
    align-items: center;
    min-height: var(--touch);
    list-style: none;
    color: var(--accent-text);
    font-size: var(--fs-subhead);
    cursor: pointer;
  }
  .tech summary::-webkit-details-marker {
    display: none;
  }
  .lines {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 12px;
    border-radius: var(--r-sm);
    background: var(--surface-2);
  }
  .lines p {
    margin: 0;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  h3.list-header {
    padding-top: 24px;
  }
  .grow {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
  }
  label.list-row {
    cursor: pointer;
  }
  .passes select {
    width: auto;
    min-width: var(--touch);
    min-height: var(--touch);
    padding: 0 28px 0 8px;
    background-color: transparent;
    background-position: right 4px center;
    color: var(--text-3);
  }
  .figures {
    margin: 16px 0 0;
  }
  .figures dt {
    flex: 1;
  }
  .figures dd {
    margin: 0;
    color: var(--text-3);
    font-variant-numeric: tabular-nums;
  }
  .launch {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 12px;
    align-items: center;
    padding-top: 16px;
  }
  .footnote,
  .why {
    margin: 0;
  }
  .why {
    padding-top: 8px;
  }
  .err {
    margin: 0;
    padding-top: 8px;
    color: var(--negative);
    font-size: var(--fs-subhead);
  }
  .refusal {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
</style>
