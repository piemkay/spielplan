<script>
  /**
   * Rank. Spec v2.1 §6.3, §6.7, §6.8; proposals 71–83 and 157.
   *
   *   "Per-user table/board of every rated title in tiers **F, D, C, B, A, A+, S** …
   *    **Drag-and-drop rearrange** … implemented as Ledger observations … **On phones:** tap a
   *    title (it lifts), tap a tier (it drops) … **Comparison queue** ('sharpen my ranking')."
   *
   * Two input paths, one write. §6.3 gives tap-to-tier "the same `tier_edit` semantics" as the
   * pointer drag, so both end in `drop()` with the same body — a second write path would be a
   * second thing to keep in step, and the phone path is the one that would drift.
   *
   * The board is never re-sorted here. §6.3 forbids snapping back, and the client shape of that
   * failure is optimistic re-sorting: the title lands where you dropped it, the response
   * arrives, and it slides somewhere else under your thumb. So a drop waits, and the response
   * replaces the board whole.
   */
  import { onDestroy, onMount } from 'svelte';
  import { modelGate } from '$lib/home.svelte.js';
  import { session } from '$lib/session.svelte.js';
  import {
    KIND_LABELS,
    SHARPEN_LABEL,
    TAP_FOOTNOTE,
    answer,
    chipFor,
    clearFilters,
    closeQueue,
    draft,
    drop,
    dropLifted,
    emptyState,
    facets,
    lift,
    load,
    loadFacets,
    neighboursIn,
    openQueue,
    putDown,
    rank,
    reset
  } from '$lib/rank.svelte.js';

  const showModel = $derived(!!session.user?.show_model);
  const empty = $derived(emptyState());
  const lifted = $derived(rank.lifted);

  onMount(() => {
    loadFacets('movie');
    return load('movie');
  });
  // The house convention (`rate/+page.svelte`), and the reason this surface needs it more: a
  // lift is a pending write naming a bare title id, so one left armed across a navigation is a
  // `tier_edit` waiting to land wherever the next tap happens to be.
  onDestroy(reset);

  async function chooseKind(key) {
    await loadFacets(key);
    await load(key);
  }

  let lastModelEpoch = modelGate.epoch;
  $effect(() => {
    const epoch = modelGate.epoch;
    if (epoch === lastModelEpoch) return;
    lastModelEpoch = epoch;
    load(rank.kind);
  });

  /** Pointer devices only: §6.3 keeps drag-and-drop "for pointer devices" alongside the tap. */
  let dragging = $state(null);

  function onDragStart(entry, event) {
    dragging = entry;
    // Firefox refuses to start a drag unless `dataTransfer` is written synchronously here, so
    // without this line the owner's headline requirement simply does nothing in that browser —
    // and neither e2e project is Firefox, so nothing would have said so.
    event.dataTransfer?.setData('text/plain', String(entry.title_id));
    if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move';
  }

  /**
   * §6.3: "dropping a title into a tier emits a `tier_edit`; dropping it *between* two titles
   * emits that edit **plus two margin-less duels** against its new neighbours."
   *
   * `beforeTitleId` is what makes the second case reachable: dropping onto a poster means
   * "above this one", which names both neighbours. Dropping on the row's empty space means
   * "into this tier", which names one. The row is still a target, so an empty tier is still
   * droppable (proposal 82).
   */
  async function onDropInto(tierIndex, event, beforeTitleId = null) {
    event.preventDefault();
    event.stopPropagation();
    if (!dragging) return;
    const entry = dragging;
    dragging = null;
    await drop({
      title_id: entry.title_id,
      tier: tierIndex,
      ...neighboursIn(tierIndex, entry, beforeTitleId)
    });
  }
</script>

<section data-testid="rank-surface">
  <header>
    <div class="head">
      <h1>Rank</h1>
<!-- Proposal 128: the pill is the selection primitive, and `role="group"` + `aria-pressed` is
           what every other kind switcher in the app uses (Rate, Home). A `role="tab"` with no
           tabpanel and no roving tabindex announces a widget that is not there. -->
      <div class="tabs" role="group" aria-label="Kind">
        {#each Object.entries(KIND_LABELS) as [key, label] (key)}
          <button
            class="pill"
            aria-pressed={rank.kind === key}
            data-kind={key}
            onclick={() => chooseKind(key)}>{label}</button
          >
        {/each}
      </div>
    </div>
    <!-- Proposal 81: seven letters must not be presented as given. -->
    <p class="why" data-testid="rank-why">{rank.why}</p>
  </header>

  <div class="controls">
    <input
      type="search"
      placeholder="filter — title or DNA term, e.g. cosy"
      bind:value={draft.q}
      onchange={() => load(rank.kind)}
      data-testid="rank-filter"
    />
    <input
      type="text"
      placeholder="DNA term (mood.cosy)"
      bind:value={draft.dna}
      onchange={() => load(rank.kind)}
      data-testid="rank-dna"
    />
    <select bind:value={draft.genre} onchange={() => load(rank.kind)} data-testid="rank-genre">
      <option value="">every genre</option>
      {#each facets.genres as g (g)}<option value={g}>{g}</option>{/each}
    </select>
    <select bind:value={draft.decade} onchange={() => load(rank.kind)} data-testid="rank-decade">
      <option value="">every decade</option>
      {#each facets.decades as d (d)}<option value={d}>{d}s</option>{/each}
    </select>
    <input
      type="number"
      min="1"
      placeholder="max minutes"
      bind:value={draft.runtime_max}
      onchange={() => load(rank.kind)}
      data-testid="rank-runtime"
    />
    <select bind:value={draft.seen} onchange={() => load(rank.kind)} data-testid="rank-seen">
      <option value="any">seen or not</option>
      <option value="seen">seen</option>
      <option value="unseen">not seen</option>
    </select>
    <button
      class="pill sharpen"
      onclick={openQueue}
      data-testid="rank-sharpen"
      disabled={rank.busy || rank.ratedTotal < 2}>{SHARPEN_LABEL}</button
    >
  </div>
  <!-- Decision 35's rule, generalised: a control that disables has to say why, or the person
       reads a dead button as a broken one. `queue_eligible` is the server's own answer. -->
  {#if rank.booted && rank.ratedTotal < 2}
    <p class="why" data-testid="rank-sharpen-why">
      Nothing to compare yet — the queue draws from titles you have rated.
    </p>
  {:else if rank.booted && rank.queueEligible === 0}
    <p class="why" data-testid="rank-sharpen-why">
      {rank.ratedTotal} rated · nothing straddles a boundary right now, so the queue is
      exploring rather than settling one.
    </p>
  {/if}

  {#if rank.error}
    <p class="error" role="alert">{rank.error}</p>
  {/if}
  {#if rank.notice}
    <p class="notice" role="status">{rank.notice}</p>
  {/if}

  {#if lifted}
    <!-- Proposals 74 and 75: the banner, the Cancel, and the standing footnote. A modeless
         lift with an undiscoverable exit is the classic tap-to-move failure. -->
    <div class="moving" data-testid="rank-moving" role="status">
      <span>Moving <strong>{lifted.name}</strong> — tap a tier to drop it.</span>
      <button onclick={putDown} data-testid="rank-cancel-lift">Cancel</button>
    </div>
  {/if}

  {#if rank.loading}
    <p class="why" data-testid="rank-loading">reading your board…</p>
  {/if}

  {#if empty}
    <p class="empty" data-testid="rank-empty">
      {empty.text}
      {#if empty.kind === 'no-match'}
        <button onclick={clearFilters} data-testid="rank-clear">{empty.cta}</button>
      {:else}
        <a href="/rate">{empty.cta}</a>
      {/if}
    </p>
  {/if}

  <!-- Proposal 82: best-first, and empty tiers stay on screen as valid drop targets. -->
  <div class="board" class:armed={!!lifted} data-testid="rank-board">
    {#each rank.tiers as tier (tier.index)}
      <div
        class="row"
        class:arm={!!lifted}
        data-tier={tier.label}
        data-tier-index={tier.index}
        ondragover={(e) => e.preventDefault()}
        ondrop={(e) => onDropInto(tier.index, e)}
        role="group"
        aria-label={tier.label}
      >
        <button
          class="gutter data-lg"
          onclick={() => dropLifted(tier.index)}
          disabled={!lifted || rank.busy}
          data-testid={`rank-tier-${tier.label}`}
          aria-label={`Drop into ${tier.label}`}>{tier.label}</button
        >
        <div class="tray">
          {#each tier.entries as entry (entry.title_id)}
            {@const chip = chipFor(entry)}
            <button
              class="tile"
              class:picked={lifted?.title_id === entry.title_id}
              class:dim={lifted && lifted.title_id !== entry.title_id}
              draggable="true"
              ondragstart={(e) => onDragStart(entry, e)}
              ondragend={() => (dragging = null)}
              ondragover={(e) => e.preventDefault()}
              ondrop={(e) => onDropInto(tier.index, e, entry.title_id)}
              onclick={() => lift(entry)}
              disabled={rank.busy}
              aria-pressed={lifted?.title_id === entry.title_id}
              data-title={entry.title_id}
              data-testid={`rank-title-${entry.title_id}`}
              title={entry.badge}
            >
              <span class="name">{entry.name}</span>
              <span class="why badge">{entry.badge}</span>
              {#if chip}
                <span
                  class="chip"
                  class:tension={chip.kind === 'tension'}
                  data-chip={chip.kind}
                  data-testid={`rank-chip-${entry.title_id}`}>{chip.text}</span
                >
              {/if}
              <!-- §4.1 rule 1: the two DNA tiers "must stay distinguishable", and a survivor of
                   a DNA predicate that does not say whether the match was quote-verified or
                   inferred has merged them where it matters — in the answer a person reads. -->
              {#if rank.dnaTiers?.[entry.title_id]}
                <span class="why tiers" data-testid={`rank-dna-${entry.title_id}`}
                  >{rank.dnaTiers[entry.title_id].join(' + ')}</span
                >
              {/if}
            </button>
          {/each}
        </div>
      </div>
    {/each}
  </div>

  <p class="why foot">{TAP_FOOTNOTE}</p>

  {#if showModel && rank.model}
    <div class="model data" data-testid="rank-model">
      cutpoints [{rank.model.cutpoints.map((c) => c.toFixed(2)).join(' · ')}] · straddle_z
      {rank.model.straddle_z} · tension {rank.model.tension_credible_mass} · held-out
      {rank.model.held_out.pairs} pairs
      {#if rank.model.held_out.rate !== null}· agreement {rank.model.held_out.rate.toFixed(2)}{/if}
    </div>
  {/if}
  {#if showModel && rank.log.length}
    <ul class="log data" data-testid="rank-log">
      {#each rank.log as line}<li>{line}</li>{/each}
    </ul>
  {/if}
</section>

{#if rank.queueOpen}
  <!-- Proposal 73's screen. §6.3's queue reuses §6.1's Battle pattern: two posters are the
       buttons, with the mirrored left | about the same | right strip. -->
  <div class="queue" data-testid="rank-queue">
    <div class="queue-head">
      <strong>{SHARPEN_LABEL}</strong>
      <button onclick={closeQueue} data-testid="rank-queue-close">Done</button>
    </div>
    {#if rank.pair}
      <p class="why" data-testid="rank-pair-reason">{rank.pair.reason}</p>
      <div class="pair">
        <button
          class="side"
          onclick={() => answer('A')}
          disabled={rank.busy}
          data-testid="rank-pair-a">{rank.pair.name_a}</button
        >
        <button
          class="tie"
          onclick={() => answer('TIE')}
          disabled={rank.busy}
          data-testid="rank-pair-tie">about the same</button
        >
        <button
          class="side"
          onclick={() => answer('B')}
          disabled={rank.busy}
          data-testid="rank-pair-b">{rank.pair.name_b}</button
        >
      </div>
    {:else}
      <p class="empty" data-testid="rank-queue-empty">{rank.queueReason}</p>
    {/if}
  </div>
{/if}

<style>
  section {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  h1 {
    margin: 0;
    font-size: 21px;
    font-weight: 600;
  }
  .tabs {
    display: flex;
    gap: 4px;
  }
  .controls button,
  .controls select,
  .controls input {
    min-height: 36px;
    padding: 0 11px;
    border: 1px solid var(--line-2);
    border-radius: var(--r-pill);
    background: var(--card);
    color: var(--ink-2);
    font-size: 12.5px;
  }
  .controls {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .controls input[type='number'] {
    max-width: 128px;
  }
  .sharpen {
    border-color: var(--ember-edge);
    background: var(--ember-wash);
    color: var(--ember-lift);
  }
  .sharpen:disabled {
    opacity: 0.55;
  }

  .moving {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 9px 12px;
    border: 1px solid var(--ember-edge);
    background: var(--ember-wash);
    border-radius: var(--r-sm);
    font-size: 13px;
  }
  .moving button {
    min-height: 32px;
    padding: 0 12px;
    border: 1px solid var(--ember-edge);
    border-radius: var(--r-pill);
    background: transparent;
    color: var(--ember-lift);
  }
  .board {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .row {
    display: flex;
    gap: 8px;
    align-items: stretch;
    border: 1px solid transparent;
    border-radius: var(--r-sm);
  }
  /* Proposal 75: while a title is lifted, every tier row is visibly armed. */
  .row.arm {
    border-color: var(--ember-edge);
  }
  .gutter {
    flex: none;
    width: 52px;
    border: 1px solid var(--line);
    border-radius: var(--r-sm);
    background: var(--card);
    color: var(--ink-2);
    font-size: 13px;
  }
  .gutter:disabled {
    cursor: default;
  }
  .tray {
    flex: 1;
    min-height: 64px;
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    padding: 6px;
    border: 1px solid var(--line);
    border-radius: var(--r-sm);
    background: var(--ground-raised);
  }
  /* NOT `.poster`: `design.css`'s global `.poster` is the 2:3 card (`aspect-ratio: 2/3;
   * overflow: hidden`), which forced every text chip to 1.5x its own width and clipped the
   * badge §6.3 requires. The two components that want that card re-declare it in their own
   * scoped styles; this one wants a chip, so it does not borrow the name. */
  .tile {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 2px;
    min-height: var(--touch);
    max-width: 220px;
    padding: 6px 9px;
    border: 1px solid var(--line-2);
    border-radius: var(--r-sm);
    background: var(--card);
    text-align: left;
  }
  .tile.picked {
    border-color: var(--ember);
  }
  .tile.dim {
    opacity: 0.5;
  }
  .name {
    font-size: 13px;
    color: var(--ink);
  }
  .badge {
    font-size: 10px;
  }
  .chip {
    margin-top: 2px;
    padding: 1px 6px;
    border: 1px solid var(--line-2);
    border-radius: var(--r-pill);
    font-family: var(--mono);
    font-size: 10px;
    color: var(--ink-3);
  }
  .chip.tension {
    border-color: var(--ember-edge);
    color: var(--ember-lift);
  }
  .tiers {
    font-size: 10px;
  }
  .foot {
    padding-top: 6px;
    border-top: 1px solid var(--line);
  }
  .error {
    color: var(--ember-lift);
    font-size: 13px;
  }
  .notice {
    color: var(--ink-2);
    font-size: 13px;
  }
  .empty {
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 13px;
    color: var(--ink-2);
  }
  .empty button {
    min-height: 32px;
    padding: 0 12px;
    border: 1px solid var(--line-2);
    border-radius: var(--r-pill);
    background: var(--card);
  }
  .model,
  .log {
    padding: 8px 10px;
    border: 1px solid var(--line);
    border-radius: var(--r-sm);
    background: var(--card);
  }
  .log {
    margin: 0;
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .queue {
    position: fixed;
    inset: auto 0 0 0;
    padding: 14px;
    border-top: 1px solid var(--line-2);
    background: var(--card-raised);
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .queue-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .queue-head button {
    min-height: 36px;
    padding: 0 14px;
    border: 1px solid var(--line-2);
    border-radius: var(--r-pill);
    background: var(--card);
  }
  .pair {
    display: grid;
    grid-template-columns: 1fr auto 1fr;
    gap: 8px;
    align-items: stretch;
  }
  .pair button {
    min-height: var(--touch);
    padding: 10px;
    border: 1px solid var(--line-2);
    border-radius: var(--r-sm);
    background: var(--card);
    font-size: 13px;
  }
  .pair .tie {
    color: var(--ink-3);
    font-size: 12px;
  }

  /* §6 preamble: "48 px targets". `design.css` sets this globally for `button`/`select`, but a
   * scoped rule outranks it, so this page has to re-declare it — the way RateCorrections,
   * RateUndo and ShelfRow each do. LAST in the sheet on purpose: these selectors tie on
   * specificity with the base rules above, so source order is what decides, and an override
   * placed earlier loses to the 32 px it is meant to beat. The e2e touch-floor test on the
   * phone project is what caught that. */
  @media (pointer: coarse) {
    .controls button,
    .controls select,
    .controls input,
    .moving button,
    .empty button,
    .queue-head button,
    .pair button {
      min-height: var(--touch);
    }
  }
</style>
