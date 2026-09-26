<script>
  // Tap-to-tier and pointer drag both end in `drop()` with one body (§6.3). The board is never
  // re-sorted here: a drop waits and the response replaces it whole, so nothing snaps back.
  import { onDestroy, onMount } from 'svelte';
  import RatePoster from '$lib/components/RatePoster.svelte';
  import TitleDetail from '$lib/components/TitleDetail.svelte';
  import { modelGate } from '$lib/home.svelte.js';
  import { session } from '$lib/session.svelte.js';
  import {
    KIND_LABELS,
    ROUND_END_TEXT,
    SHARPEN_LABEL,
    TAP_FOOTNOTE,
    answer,
    chipFor,
    chooseKind,
    clearFilters,
    closeQueue,
    closeTitle,
    dnaTierText,
    draft,
    drop,
    dropLifted,
    emptyState,
    sharpenWhy,
    facets,
    keepGoing,
    lift,
    load,
    loadFacets,
    neighboursIn,
    openQueue,
    putDown,
    rank,
    reset,
    roundLine,
    tapTile,
    typed
  } from '$lib/rank.svelte.js';

  const showModel = $derived(!!session.user?.show_model);
  const empty = $derived(emptyState());
  const why = $derived(sharpenWhy());
  const lifted = $derived(rank.lifted);
  // The last answer's two titles, marked on the board while the sheet is up.
  const compared = $derived(new Set((rank.placed ?? []).map((p) => p.title_id)));

  // The sheet is fixed over the scrolling `main`, so a spacer of its height keeps the board reachable.
  let sheetHeight = $state(0);

  onMount(() => {
    loadFacets('movie');
    return load('movie');
  });
  // A lift is a pending write, so it must not survive a navigation.
  onDestroy(reset);

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
    // Firefox will not start a drag unless `dataTransfer` is written synchronously.
    event.dataTransfer?.setData('text/plain', String(entry.title_id));
    if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move';
  }

  // Dropping on a poster means "above this one" and names both neighbours; on the row's empty
  // space it is a drop into the tier (§6.3).
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
      <!-- A pill group with aria-pressed, like every kind switch; role="tab" would promise a tabpanel. -->
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
    <p class="why" data-testid="rank-why">{rank.why}</p>
  </header>

  <div class="controls">
    <!-- The title box searches names only; the tag box takes a tag's label or id. -->
    <!-- The tag box keeps `change`: it matches a whole tag, so each keystroke would empty the board. -->
    <input
      type="search"
      placeholder="filter by title"
      aria-label="filter by title"
      bind:value={draft.q}
      oninput={typed}
      data-testid="rank-filter"
    />
    <input
      type="text"
      placeholder="tag, e.g. cosy"
      aria-label="tag"
      bind:value={draft.dna}
      onchange={() => load(rank.kind)}
      data-testid="rank-dna"
    />
    <label class="field">
      <span class="caption">genre</span>
      <select bind:value={draft.genre} onchange={() => load(rank.kind)} data-testid="rank-genre">
        <option value="">every genre</option>
        {#each facets.genres as g (g)}<option value={g}>{g}</option>{/each}
      </select>
    </label>
    <label class="field">
      <span class="caption">decade</span>
      <select bind:value={draft.decade} onchange={() => load(rank.kind)} data-testid="rank-decade">
        <option value="">every decade</option>
        {#each facets.decades as d (d)}<option value={d}>{d}s</option>{/each}
      </select>
    </label>
    <label class="field">
      <span class="caption">max minutes</span>
      <input
        type="number"
        min="1"
        inputmode="numeric"
        placeholder="any"
        bind:value={draft.runtime_max}
        oninput={typed}
        data-testid="rank-runtime"
      />
    </label>
    <label class="field">
      <span class="caption">seen</span>
      <select bind:value={draft.seen} onchange={() => load(rank.kind)} data-testid="rank-seen">
        <option value="any">seen or not</option>
        <option value="seen">seen</option>
        <option value="unseen">not seen</option>
      </select>
    </label>
    <button
      class="pill sharpen"
      onclick={openQueue}
      data-testid="rank-sharpen"
      disabled={rank.busy || rank.ratedTotal < 2}>{SHARPEN_LABEL}</button
    >
  </div>
  <!-- A control that disables has to say why. -->
  {#if why}
    <p class="why" data-testid="rank-sharpen-why" data-why-kind={why.kind}>{why.text}</p>
  {/if}

  {#if rank.error}
    <p class="error" role="alert">{rank.error}</p>
  {/if}
  {#if rank.notice}
    <p class="notice" role="status">{rank.notice}</p>
  {/if}

  {#if lifted}
    <!-- Sticky, so Cancel stays on screen for a Move deep in a long tier. -->
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

  <!-- Empty tiers stay on screen as drop targets. -->
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
        <!-- The whole row height is the drop target; the letter sticks at its top. -->
        <button
          class="gutter"
          onclick={() => dropLifted(tier.index)}
          disabled={!lifted || rank.busy}
          data-testid={`rank-tier-${tier.label}`}
          aria-label={`Drop into ${tier.label}`}
        >
          <span class="label">
            <span class="letter data-lg" data-testid={`rank-letter-${tier.label}`}>{tier.label}</span>
            <span class="count data">{tier.entries.length}</span>
          </span>
        </button>
        <div class="tray">
          {#each tier.entries as entry (entry.title_id)}
            {@const chip = chipFor(entry)}
            <div
              class="tile"
              class:picked={lifted?.title_id === entry.title_id}
              class:dim={lifted && lifted.title_id !== entry.title_id}
              class:compared={compared.has(entry.title_id)}
              draggable="true"
              ondragstart={(e) => onDragStart(entry, e)}
              ondragend={() => (dragging = null)}
              ondragover={(e) => e.preventDefault()}
              ondrop={(e) => onDropInto(tier.index, e, entry.title_id)}
              role="group"
              aria-label={entry.name}
              data-title={entry.title_id}
              data-testid={`rank-title-${entry.title_id}`}
            >
              <button
                class="open"
                onclick={() => tapTile(entry, tier.index)}
                disabled={rank.busy}
                data-testid={`rank-open-${entry.title_id}`}
                title={entry.badge}
              >
                <span class="name">{entry.name}</span>
                <span class="why badge">{entry.badge}</span>
                <!-- The two DNA tiers must stay distinguishable (§4.1 rule 1). -->
                {#if rank.dnaTiers?.[entry.title_id]}
                  <span class="why tiers" data-testid={`rank-dna-${entry.title_id}`}
                    >{dnaTierText(rank.dnaTiers[entry.title_id])}</span
                  >
                {/if}
              </button>
              {#if chip}
                <!-- The chip is its own button: the queue's entry point (§6.3), tension chip included. -->
                <button
                  class="chip"
                  class:tension={chip.kind === 'tension'}
                  onclick={openQueue}
                  disabled={rank.busy}
                  data-chip={chip.kind}
                  data-testid={`rank-chip-${entry.title_id}`}
                  aria-label={`${chip.text} — compare titles to settle it`}>{chip.text}</button
                >
              {/if}
              <button
                class="move"
                onclick={() => lift(entry)}
                disabled={rank.busy}
                aria-pressed={lifted?.title_id === entry.title_id}
                aria-label={`Move ${entry.name}`}
                data-testid={`rank-move-${entry.title_id}`}>Move</button
              >
            </div>
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

  {#if rank.queueOpen}
    <div class="sheet-room" style:height={`${sheetHeight}px`} aria-hidden="true"></div>
  {/if}
</section>

{#if rank.queueOpen}
  <div class="queue" data-testid="rank-queue" bind:clientHeight={sheetHeight}>
    <div class="queue-head">
      <strong>{SHARPEN_LABEL}</strong>
      <span class="data round" data-testid="rank-round">{roundLine()}</span>
      <button onclick={closeQueue} data-testid="rank-queue-close">Done</button>
    </div>
    {#if rank.roundDone}
      <div class="round-end" data-testid="rank-round-end">
        <p>{ROUND_END_TEXT}</p>
        <div class="round-actions">
          <button onclick={closeQueue} data-testid="rank-round-stop">Done</button>
          <button class="more" onclick={keepGoing} data-testid="rank-round-more">Keep going</button>
        </div>
      </div>
    {:else if rank.pair}
      <p class="why" data-testid="rank-pair-reason">{rank.pair.reason}</p>
      {#if showModel && rank.pair.model}
        <p class="data" data-testid="rank-pair-arm">
          {rank.pair.model.arm} · {rank.pair.model.reason}
        </p>
      {/if}
      <div class="pair">
        <button
          class="side"
          onclick={() => answer('A')}
          disabled={rank.busy}
          aria-label={`Pick ${rank.pair.name_a}`}
          data-testid="rank-pair-a"
        >
          <RatePoster title={{ id: rank.pair.title_a, name: rank.pair.name_a }} />
        </button>
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
          aria-label={`Pick ${rank.pair.name_b}`}
          data-testid="rank-pair-b"
        >
          <RatePoster title={{ id: rank.pair.title_b, name: rank.pair.name_b }} />
        </button>
      </div>
    {:else}
      <p class="empty" data-testid="rank-queue-empty">{rank.queueReason}</p>
    {/if}
    {#if rank.placed.length}
      <!-- Placement, never "moved", whichever arm drew the pair (§13). -->
      <div class="placed" data-testid="rank-placed">
        <span class="data">where they sit now</span>
        {#each rank.placed as spot (spot.title_id)}
          <span class="why" data-testid={`rank-placed-${spot.title_id}`}
            >{spot.name}: {spot.badge}</span
          >
        {/each}
      </div>
    {/if}
  </div>
{/if}

{#if rank.opened !== null}
  <!-- A credit tap closes the card (Rank has no list to filter); a seen change re-reads the board. -->
  <TitleDetail
    titleId={rank.opened}
    onClose={closeTitle}
    onPerson={closeTitle}
    onStateChange={() => load(rank.kind)}
  />
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
    /* Captioned pickers are taller, so the rest bottom-align with their boxes. */
    align-items: flex-end;
    gap: 6px;
  }
  .field {
    display: flex;
    flex-direction: column;
    gap: 3px;
    min-width: 0;
  }
  .caption {
    padding-left: 12px;
    color: var(--ink-3);
    font-size: 11px;
  }
  .controls input[type='number'] {
    max-width: 128px;
  }
  /* A native select is as wide as its longest option, which can outgrow a phone. */
  .controls select {
    max-width: 100%;
    min-width: 0;
    text-overflow: ellipsis;
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
    position: sticky;
    top: 0;
    z-index: 2;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 9px 12px;
    border: 1px solid var(--ember-edge);
    /* Opaque under the wash, because a sticky banner scrolls over the board. */
    background: linear-gradient(var(--ember-wash), var(--ember-wash)), var(--ground);
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
  .row.arm {
    border-color: var(--ember-edge);
  }
  /* The letter sticks at the top while a long tier scrolls; the full-height button is the drop target. */
  .gutter {
    flex: none;
    width: 52px;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: flex-start;
    padding: 10px 0;
    border: 1px solid var(--line);
    border-radius: var(--r-sm);
    background: var(--card);
    color: var(--ink-2);
    font-size: 13px;
  }
  .gutter .label {
    position: sticky;
    top: 8px;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 2px;
  }
  .gutter .letter {
    color: var(--ink);
  }
  .gutter .count {
    font-size: 11px;
    color: var(--ink-4);
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
  /* Not `.poster`: design.css's global `.poster` is the 2:3 card and would clip this chip. */
  .tile {
    display: flex;
    /* For the tension chip alone, which takes a row of its own (see `.chip.tension`). */
    flex-wrap: wrap;
    align-items: stretch;
    min-height: var(--touch);
    max-width: 340px;
    border: 1px solid var(--line-2);
    border-radius: var(--r-sm);
    background: var(--card);
  }
  @media (max-width: 480px) {
    .tile {
      flex: 1 1 100%;
      max-width: 100%;
    }
  }
  .tile.picked {
    border-color: var(--ember);
  }
  .tile.dim {
    opacity: 0.5;
  }
  .tile.compared {
    border-color: var(--line-3);
  }
  .open {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 2px;
    padding: 6px 9px;
    border: none;
    background: none;
    color: inherit;
    text-align: left;
    cursor: pointer;
  }
  .move {
    flex: none;
    align-self: stretch;
    padding: 0 10px;
    border: none;
    border-left: 1px solid var(--line);
    background: none;
    color: var(--ink-3);
    font-size: 11px;
  }
  .move[aria-pressed='true'] {
    color: var(--ember-lift);
  }
  .name {
    font-size: 13px;
    color: var(--ink);
  }
  .badge {
    font-size: 10px;
  }
  .chip {
    flex: none;
    align-self: center;
    margin: 0 4px;
    padding: 1px 8px;
    min-height: 28px;
    border: 1px solid var(--line-2);
    border-radius: var(--r-pill);
    background: none;
    font-family: var(--mono);
    font-size: 10px;
    color: var(--ink-3);
    max-width: 40%;
    white-space: normal;
    text-align: left;
  }
  /* The tension chip carries a whole sentence, so it takes its own full-width row. */
  .chip.tension {
    border-color: var(--ember-edge);
    color: var(--ember-lift);
    order: 1;
    flex: 1 1 100%;
    max-width: none;
    margin: 0 6px 6px;
    border-radius: var(--r-sm);
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
    /* Above the sticky lift banner, below the title card (TitleDetail's 50). */
    z-index: 10;
    padding: 14px;
    /* This sheet covers NavRail, so it pads for the home indicator itself. */
    padding-bottom: max(14px, env(safe-area-inset-bottom));
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
    gap: 10px;
  }
  .queue-head strong {
    white-space: nowrap;
  }
  .queue-head .round {
    margin-left: auto;
    color: var(--ink-3);
    font-size: 11px;
    white-space: nowrap;
  }
  .queue-head button,
  .round-actions button {
    min-height: 36px;
    padding: 0 14px;
    border: 1px solid var(--line-2);
    border-radius: var(--r-pill);
    background: var(--card);
  }
  .round-end p {
    margin: 0 0 10px;
    font-size: 14px;
  }
  .round-actions {
    display: flex;
    gap: 8px;
  }
  .round-actions .more {
    border-color: var(--ember-edge);
    background: var(--ember-wash);
    color: var(--ember-lift);
  }
  /* Small posters: the sheet sits over the board it is sharpening. */
  .pair {
    display: grid;
    grid-template-columns: minmax(0, 100px) auto minmax(0, 100px);
    justify-content: center;
    gap: 10px;
    align-items: center;
  }
  .pair button {
    min-height: var(--touch);
    border-radius: var(--r-sm);
    font-size: 13px;
  }
  .pair .side {
    padding: 0;
    border: none;
    background: none;
    color: inherit;
  }
  .pair .tie {
    padding: 10px;
    border: 1px solid var(--line-2);
    background: var(--card);
    color: var(--ink-3);
    font-size: 12px;
  }
  .placed {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding-top: 8px;
    border-top: 1px solid var(--line);
  }
  .placed .data {
    font-size: 11px;
    color: var(--ink-4);
  }

  /* Scoped rules outrank design.css's coarse floor, so restate it here, last: source order decides. */
  @media (pointer: coarse) {
    .controls button,
    .controls select,
    .controls input,
    .moving button,
    .empty button,
    .empty a,
    .queue-head button,
    .round-actions button,
    .pair button,
    .chip {
      min-height: var(--touch);
    }
    /* design.css's coarse floor raises height only; these standalone controls need the width too. */
    .move,
    .chip {
      min-width: var(--touch);
    }
    /* A standalone CTA, not prose, so it takes the floor; inline-flex, as min-height skips inline boxes. */
    .empty a {
      display: inline-flex;
      align-items: center;
    }
    /* iOS Safari zooms on focus below 16px, and this scoped select outranks design.css's 16px rule. */
    .controls select {
      font-size: 16px;
    }
  }
</style>
