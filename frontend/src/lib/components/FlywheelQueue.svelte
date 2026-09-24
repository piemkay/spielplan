<script>
  /**
   * §6.6 Data's extraction queue with its approve/spend controls. Spec v2.1 §8.4, §6.6 Data;
   * decisions 330, 441, 442 and 443.
   *
   * THE BATCH TOTAL IS THE CONTROL (plan C2). One tap can select every row, so the figure beside
   * Launch is the batch's reservation of both attempts at the providers and passes chosen for this
   * batch, against what the month has left - and it is the server's figure, asked again on every
   * change to the selection, the providers or the passes. The per-title estimate is shown beside
   * it as reassurance, never as the thing Launch is held against.
   *
   * LAUNCH IS DARK WITH ITS REASON, NEVER A WARNING AFTER THE FACT (plan C3). The reason is the
   * quote's own sentence, printed as text beside the button so a phone - which has no hover to
   * show a tooltip - reads it too, and it stays on screen when the queue is empty, because "no
   * spend cap is configured" is worth knowing before the first row arrives. The launch route prices
   * the batch again inside its transaction whatever this card shows (decision 441).
   *
   * A ROW'S REASON IS SHOWN AS WRITTEN (§6.6 Data: "each labelled with its reason"); its writer in
   * `flywheel/` composed it for this screen. A running row also shows its title's board status and
   * reason, because the row closes only at the title's next stage-8 observation (decision 440) and
   * the board is where a batch that stalled says why.
   *
   * AND ITS AGE, READ OFF ITS OWN `created_at` (§8.4 as v2.1.3 amends it: "a 'queued just now'
   * marker read off the row's own creation time"). The clock it is read against ticks while the page
   * is open, so a row that said "just now" when it arrived does not go on saying it an hour later.
   * [M5.6 review cycle 1, M56-DATA-01]
   */
  import { onDestroy, onMount } from 'svelte';
  import { get, post } from '$lib/api.js';
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

  // How long a burst of taps settles before the quote is asked: long enough that ticking five rows
  // asks once, short enough that the figure follows the finger.
  const QUOTE_DEBOUNCE_MS = 200;
  // How often the queued marker's clock moves: its finest unit is a minute, so half of one keeps
  // "just now" from outliving the minute it means by more than that.
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
      // A row that closed or launched since it was ticked leaves the selection, so the total never
      // counts a row the queue no longer shows.
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

  // Every change to the selection, the providers or the passes asks again, and the request carries
  // a snapshot of the three so its answer can be matched against what is on screen when it lands.
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
      launched = `Launched batch ${answer.batch.id}: ${answer.items.length} row(s) now running.`;
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

<div class="flywheel" data-testid="flywheel-queue">
  <h2>Extraction flywheel</h2>
  <p class="why">
    Naming failures future extraction spend should fix, each with the reason it was queued. Select
    rows, choose the providers and passes for this batch, and launch it within the monthly cap.
  </p>

  {#if error}
    <p class="err">{error}</p>
  {:else if !envelope}
    <p class="data">loading...</p>
  {:else}
    {#if items.length === 0}
      <p class="why">The queue is empty.</p>
    {/if}
    <ul class="items">
      {#each items as item (item.id)}
        <li class="item card" class:picked={selected.has(item.id)} data-testid="flywheel-row">
          <label class="pick">
            <input
              type="checkbox"
              checked={selected.has(item.id)}
              onchange={() => pick(item.id)}
              aria-label="Select row {item.id}"
            />
          </label>
          <div class="body">
            <div class="head">
              <span class="data-lg">{kindLabel(item.kind)}</span>
              <span class="data">{item.status}</span>
              {#if queuedMarker(item.created_at, now)}
                <span class="data" data-testid="flywheel-queued">{queuedMarker(item.created_at, now)}</span>
              {/if}
            </div>
            {#if titleOf(item)}<span class="name">{titleOf(item)}</span>{/if}
            <p class="reason" data-testid="flywheel-reason">{item.reason}</p>
            {#if item.status === 'running' && item.board}
              <p class="data">title's board: {item.board.status} at stage {item.board.stage}</p>
              {#if item.board.reason != null}
                <p class="reason">{item.board.reason}</p>
              {/if}
            {/if}
          </div>
        </li>
      {/each}
    </ul>

    <div class="plan">
      <fieldset class="providers">
        <legend class="data">PROVIDERS FOR THIS BATCH</legend>
        {#each providers as provider (provider.name)}
          <label class="provider">
            <input
              type="checkbox"
              checked={chosen.includes(provider.name)}
              disabled={!provider.configured}
              onchange={(e) => chooseProvider(provider.name, e.currentTarget.checked)}
            />
            <span>{provider.name}</span>
          </label>
          {#if provider.reason}
            <p class="note">{provider.name}: {provider.reason}</p>
          {/if}
        {/each}
      </fieldset>
      <label class="passes">
        <span class="data">PASSES</span>
        <select value={passes} onchange={(e) => (passes = Number(e.currentTarget.value))}>
          {#each passChoices(envelope.defaults?.passes) as n (n)}
            <option value={n}>{n}</option>
          {/each}
        </select>
      </label>
    </div>

    <dl class="figures" data-testid="flywheel-figures">
      <div>
        <dt class="data">titles selected</dt>
        <dd data-testid="flywheel-titles">{current.titles}</dd>
      </div>
      <div><dt class="data">per title</dt><dd>{dollars(figures?.per_title_usd)}</dd></div>
      <div><dt class="data">batch total</dt><dd>{dollars(figures?.total_usd)}</dd></div>
      <div>
        <dt class="data">reserved (both attempts)</dt>
        <dd data-testid="flywheel-reserved">{dollars(figures?.reserved_usd)}</dd>
      </div>
      <div>
        <dt class="data">left this month</dt>
        <dd>{roomLeft(figures, envelope.meter)}</dd>
      </div>
    </dl>

    <div class="launch">
      <button
        class="btn-primary"
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
    {#if quoteError}<p class="err">{quoteError}</p>{/if}
    {#if launchRefusal}<p class="err refusal" data-testid="flywheel-refusal">{launchRefusal}</p>{/if}
    {#if launched}<p class="why">{launched}</p>{/if}
  {/if}
</div>

<style>
  .flywheel {
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
  .items {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .item {
    display: flex;
    gap: 10px;
    align-items: flex-start;
  }
  /* A selected row is marked by its box's tick and a brighter edge in ink, not by the ember: the
     only accent on this card is Launch, the one primary action, so the eye is not asked to tell a
     selection from the button it arms by colour alone (§6.8; decision 276). */
  .item.picked {
    border-color: var(--ink-3);
  }
  .pick {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex: none;
    cursor: pointer;
  }
  .body {
    display: flex;
    flex-direction: column;
    gap: 4px;
    min-width: 0;
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
  /* Whole and wrapped, for the board's reason: a queue row's reason is the sentence the operator
     decides to spend on, and the launch refusal is the sentence that says why they cannot. */
  .reason {
    margin: 0;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    font-size: 13px;
    line-height: 1.5;
    color: var(--ink-2);
  }
  .plan {
    display: flex;
    flex-wrap: wrap;
    gap: 12px 24px;
    align-items: flex-start;
  }
  .providers {
    border: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .provider {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    cursor: pointer;
  }
  .note {
    margin: 0;
    font-size: 12px;
    line-height: 1.45;
    color: var(--ink-4);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .passes {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .figures {
    margin: 0;
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
    gap: 8px;
  }
  .figures dd {
    margin: 2px 0 0;
    font-family: var(--mono);
    font-size: 12px;
    color: var(--ink-2);
  }
  .launch {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 12px;
    align-items: center;
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
    /* §6 preamble's 48 px floor, on both axes. A checkbox is the control that ships at 16 px, and
       design.css's coarse block reaches neither a label nor a checkbox, so the label - which is
       what a thumb actually lands on - carries the whole target itself. */
    .pick {
      min-height: var(--touch);
      min-width: var(--touch);
    }
    .provider {
      min-height: var(--touch);
      min-width: var(--touch);
    }
    /* The pass picker is a `select`, which design.css raises on height and never on width, and one
       digit leaves it about 41 px wide by content - on the control that doubles the reservation
       when it goes from 1 to 2. [M5.6 review cycle 1, M56-DATA-04] */
    .passes select {
      min-width: var(--touch);
    }
  }
</style>
