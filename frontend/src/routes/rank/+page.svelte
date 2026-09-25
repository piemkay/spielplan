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
   * A tap on a title opens it; moving is the Move control beside it (decision 496). Each tile is
   * therefore a group of buttons rather than one button, because the open area, the chip that
   * opens the queue (§6.3, "the badge is the queue's entry point") and Move are three different
   * things and a control nested inside another control is none of them reliably.
   *
   * The board is never re-sorted here. §6.3 forbids snapping back, and the client shape of that
   * failure is optimistic re-sorting: the title lands where you dropped it, the response
   * arrives, and it slides somewhere else under your thumb. So a drop waits, and the response
   * replaces the board whole.
   */
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
    tapTile
  } from '$lib/rank.svelte.js';

  const showModel = $derived(!!session.user?.show_model);
  const empty = $derived(emptyState());
  const why = $derived(sharpenWhy());
  const lifted = $derived(rank.lifted);
  // The two titles the last answer was about, marked on the board while the sheet is up, so the
  // part of the board above it shows where they landed. Placement only, the same on every arm
  // (`read.placements`); closing the sheet clears it with the round.
  const compared = $derived(new Set((rank.placed ?? []).map((p) => p.title_id)));

  // The sheet is fixed over `main`, which is the scroll container (+layout.svelte), and nothing
  // reserved room for it: the last ~110 px of the board could not be scrolled above it while it
  // was open. A spacer of the sheet's own measured height is that room. [§6 preamble]
  let sheetHeight = $state(0);

  onMount(() => {
    loadFacets('movie');
    return load('movie');
  });
  // The house convention (`rate/+page.svelte`), and the reason this surface needs it more: a
  // lift is a pending write naming a bare title id, so one left armed across a navigation is a
  // `tier_edit` waiting to land wherever the next tap happens to be.
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
    <!-- The first box searches names and aliases and nothing else (`db/library.py`'s `q`), so
         it claims nothing more: "title or DNA term, e.g. cosy" sent people typing a tag into a
         box that cannot find one, the lie Home's box shed in M4.9 (finding 19). The tag box is
         the second one, and it takes a tag by the name the title card shows for it (its label),
         or by its id, bare or facet-qualified - `db/library._dna_term_matches`. [decision 486] -->
    <input
      type="search"
      placeholder="filter by title"
      bind:value={draft.q}
      onchange={() => load(rank.kind)}
      data-testid="rank-filter"
    />
    <input
      type="text"
      placeholder="tag, e.g. cosy"
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
       reads a dead button as a broken one. `sharpenWhy` decides which reason applies, in
       rank.svelte.js beside `emptyState`, because the branch that fires during decision 209's
       window used to give the wrong one and no test could reach it here. -->
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
    <!-- Proposals 74 and 75: the banner, the Cancel, and the standing footnote. A modeless
         lift with an undiscoverable exit is the classic tap-to-move failure. Sticky inside
         `main`, because a Move tapped on a title deep in a thousand-pixel tier put the banner
         and its Cancel off-screen above it. -->
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
        <!-- The whole height of the row stays the drop target, and the letter sits at its top
             and stays in view while a long tier scrolls past: a button centres its content, and
             in a sixteen-title tier on a phone that put the letter ~500 px down the row, so the
             first household saw letters only on the empty tiers. -->
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
                <!-- §4.1 rule 1: the two DNA tiers "must stay distinguishable", and a survivor of
                     a DNA predicate that does not say whether the match was quote-verified or
                     inferred has merged them where it matters — in the answer a person reads. -->
                {#if rank.dnaTiers?.[entry.title_id]}
                  <span class="why tiers" data-testid={`rank-dna-${entry.title_id}`}
                    >{dnaTierText(rank.dnaTiers[entry.title_id])}</span
                  >
                {/if}
              </button>
              {#if chip}
                <!-- §6.3 (decision 295): "the badge is the queue's entry point". It was a span
                     inside the tile's one button, so tapping it lifted the title. The tension
                     chip opens the queue too: it replaces the straddle chip while it holds
                     (proposal 71), and a queue-eligible title in tension would otherwise have
                     no door at all. -->
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
  <!-- Proposal 73's screen. §6.3's queue reuses §6.1's Battle pattern: two posters are the
       buttons, with the mirrored left | about the same | right strip. The posters are the same
       component the battle card uses, handed a title with its `id` (decision 483). -->
  <div class="queue" data-testid="rank-queue" bind:clientHeight={sheetHeight}>
    <div class="queue-head">
      <strong>{SHARPEN_LABEL}</strong>
      <!-- Decision 495: how far through the round, in the data voice. -->
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
      <!-- Decision 117 puts "the selection label in the tier queue" behind the toggle, and the
           route has sent it there since M4.10; nothing rendered it. -->
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
      <!-- Where the two titles of the last answer sit now, as the board's own badges. The same
           words whichever arm drew the pair: placement, never "moved" (§13). -->
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
  <!-- Decision 496: the card a tap opens. Home's credit tap filters Home's own list, which
       Rank does not have, so here it closes the card; a seen-state change re-reads the board,
       because the board's filters can include seen-state. -->
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
    gap: 6px;
  }
  .controls input[type='number'] {
    max-width: 128px;
  }
  /* A native select is as wide as its longest option, and one long genre once pushed Home wider
     than an iPhone 13 (user test 2026-09-25, C7.3). Decision 473's vocabulary keeps Rank's longest
     at "Science Fiction"; this keeps any future option inside the row, as Home's
     `.filters select` does. */
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
  /* Proposal 75: while a title is lifted, every tier row is visibly armed. */
  .row.arm {
    border-color: var(--ember-edge);
  }
  /* The letter at the top of its row, not centred in it: a column laid out from the top, and a
     label that sticks 8 px below the top of `main` (the scroll container; the app header is
     outside it) while its tier scrolls past. The button stays the row's full height, because
     the whole gutter is the tap-to-drop target. */
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
  /* NOT `.poster`: `design.css`'s global `.poster` is the 2:3 card (`aspect-ratio: 2/3;
   * overflow: hidden`), which forced every text chip to 1.5x its own width and clipped the
   * badge §6.3 requires. The two components that want that card re-declare it in their own
   * scoped styles; this one wants a chip, so it does not borrow the name. */
  .tile {
    display: flex;
    align-items: stretch;
    min-height: var(--touch);
    max-width: 340px;
    border: 1px solid var(--line-2);
    border-radius: var(--r-sm);
    background: var(--card);
  }
  /* On a phone every tile took its own line anyway (a 220 px cap in a ~300 px tray); saying so
     makes the layout a decision rather than an accident, and gives the name and its badge the
     room the two controls beside them now take. */
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
    /* Above the sticky lift banner, below the title card (TitleDetail's 50). */
    z-index: 10;
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
  /* The posters are held small: the sheet sits over the board it is sharpening, and a pair of
     full-width 2:3 cards would cover most of a phone's screen. */
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
    .empty a,
    .queue-head button,
    .round-actions button,
    .pair button,
    .chip {
      min-height: var(--touch);
    }
    /* Decision 496's Move and the chip that opens the queue are standalone controls beside the
       title, so both axes of §6 preamble's floor apply to them — the coarse block above raises
       the height alone, which is how two overlay exits once shipped 48 tall and 32 wide. */
    .move,
    .chip {
      min-width: var(--touch);
    }
    /* `.empty a` is the same control as `.empty button`, one branch over. `emptyState()` sends
       `no-match` to the button and `fitting`/`unrated`/`thin` to an anchor, and the anchor is the
       first-run state every member meets — so the floor held or not depending on which sentence
       the server had sent, which no decision says and exit criterion 4 says cannot be true. A
       bare `<a>` is outside design.css's coarse list on purpose (it would grow every inline prose
       link); this is a standalone flex-item CTA, not prose, so it takes the rule here.
       `inline-flex` because `min-height` does nothing to an inline box.
       [§6 preamble; M4.15 review cycle 1] */
    .empty a {
      display: inline-flex;
      align-items: center;
    }
    /* And the size, for the three selects: iOS Safari magnifies the page on focus for any
       control below 16 px and never undoes it, which on a filter row leaves a member reading
       the board magnified with no gesture that says undo. `design.css` says 16 px for `select`
       too, but that bare selector is (0,0,1) and `.controls select` is not — the same
       specificity arithmetic the min-height above exists for. The three inputs are deliberately
       absent: design.css's four-`:not` input rule is (0,4,1) and outranks this sheet, so
       restating them here would be a second spelling of one rule. The buttons keep 12.5 px —
       a button is not focus-zoomed, and growing every label reflows the row. [finding 3] */
    .controls select {
      font-size: 16px;
    }
  }
</style>
