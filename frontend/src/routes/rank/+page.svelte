<script>
  // A tap opens the title card, Move opens an action sheet of the tiers, and a pointer can drag
  // (decision 527). The board is never re-sorted here: a drop waits and the response replaces it.
  import { onDestroy, onMount } from 'svelte';
  import ActionSheet from '$lib/components/ActionSheet.svelte';
  import RatePoster from '$lib/components/RatePoster.svelte';
  import Sheet from '$lib/components/Sheet.svelte';
  import TitleDetail from '$lib/components/TitleDetail.svelte';
  import { modelGate } from '$lib/home.svelte.js';
  import { session } from '$lib/session.svelte.js';
  import {
    KIND_LABELS,
    ROUND_END_TEXT,
    ROUND_END_TITLE,
    ROUND_SIZE,
    answer,
    chipFor,
    chooseKind,
    clearFilter,
    clearFilters,
    closeQueue,
    closeTitle,
    countLine,
    dnaTierText,
    draft,
    drop,
    emptyState,
    facets,
    filterChips,
    keepGoing,
    load,
    loadFacets,
    moveTo,
    neighboursIn,
    openQueue,
    openTitle,
    rank,
    reset,
    roundLine,
    typed
  } from '$lib/rank.svelte.js';

  const showModel = $derived(!!session.user?.show_model);
  const empty = $derived(emptyState());
  const count = $derived(countLine());
  const chips = $derived(filterChips());

  let filtersOpen = $state(false);

  /** @type {any} the row whose Move sheet is open */
  let moving = $state(null);
  const moveOptions = $derived.by(() => {
    const entry = moving;
    if (!entry) return [];
    return rank.tiers.map((tier) => ({
      label: tier.label,
      detail: tier.verdict ? `· ${tier.verdict}` : undefined,
      checked: tier.index === entry.tier,
      onSelect: () => moveTo(entry, tier.index)
    }));
  });

  onMount(() => {
    loadFacets('movie');
    return load('movie');
  });
  onDestroy(reset);

  let lastModelEpoch = modelGate.epoch;
  $effect(() => {
    const epoch = modelGate.epoch;
    if (epoch === lastModelEpoch) return;
    lastModelEpoch = epoch;
    load(rank.kind);
  });

  function metaOf(entry) {
    const matched = rank.dnaTiers?.[entry.title_id];
    return [entry.year, matched && dnaTierText(matched)].filter(Boolean).join(' · ');
  }

  /** Pointer devices only: §6.3 keeps drag-and-drop "for pointer devices" beside Move. */
  let dragging = $state(null);

  function onDragStart(entry, event) {
    dragging = entry;
    // Firefox will not start a drag unless `dataTransfer` is written synchronously.
    event.dataTransfer?.setData('text/plain', String(entry.title_id));
    if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move';
  }

  // Dropping on a title means "above this one" and names both neighbours; anywhere else in the
  // section it is a drop into the tier (§6.3).
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

<section class="rank" data-testid="rank-surface">
  <h1 class="large-title">Rank</h1>

  <div class="stack">
    <div class="segmented" role="group" aria-label="Kind">
      {#each Object.entries(KIND_LABELS) as [key, label] (key)}
        <button aria-pressed={rank.kind === key} data-kind={key} onclick={() => chooseKind(key)}
          >{label}</button
        >
      {/each}
    </div>
    {#if count}<p class="footnote count" data-testid="rank-count">{count}</p>{/if}
  </div>

  <div class="stack">
    <div class="searchrow">
      <label class="search">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="6.5" /><path d="m16 16 4.5 4.5" /></svg>
        <input
          class="q"
          type="search"
          placeholder="Search your list"
          aria-label="Search your list"
          bind:value={draft.q}
          oninput={typed}
          data-testid="rank-filter"
        />
      </label>
      <button
        class="pill"
        aria-haspopup="dialog"
        aria-expanded={filtersOpen}
        onclick={() => (filtersOpen = true)}
        data-testid="rank-filters"
      >
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h10M18 7h2M4 17h4M12 17h8" /><circle cx="16" cy="7" r="2" /><circle cx="10" cy="17" r="2" /></svg>
        {chips.length ? `Filters · ${chips.length}` : 'Filters'}
      </button>
    </div>
    {#if chips.length}
      <div class="chips">
        {#each chips as chip (chip.key)}
          <button
            class="pill on"
            onclick={() => clearFilter(chip.key)}
            aria-label={`Remove ${chip.text}`}
            data-testid={`rank-filter-chip-${chip.key}`}
          >
            {chip.text}
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18" /></svg>
          </button>
        {/each}
      </div>
    {/if}
  </div>

  {#if rank.ratedTotal >= 2}
    <div class="stack">
      <div class="card sharpen">
        <div class="sharpen-text">
          <h2 class="card-title">Sharpen your list</h2>
          <p class="why">{ROUND_SIZE} quick either-or questions</p>
        </div>
        <button
          class="btn-tinted hit start"
          onclick={openQueue}
          disabled={rank.busy}
          data-testid="rank-sharpen">Start</button
        >
      </div>
      <p class="footnote hint">Drag a title to move it, or use Move.</p>
    </div>
  {/if}

  {#if rank.error}
    <p class="error" role="alert">{rank.error}</p>
  {/if}
  {#if rank.notice && !rank.queueOpen}
    <p class="footnote" role="status">{rank.notice}</p>
  {/if}
  {#if rank.loading}
    <p class="footnote" data-testid="rank-loading">Loading your list…</p>
  {/if}

  {#if empty}
    <div class="card empty" data-testid="rank-empty">
      <p class="why">{empty.text}</p>
      {#if empty.kind === 'no-match'}
        <button class="btn-secondary" onclick={clearFilters} data-testid="rank-clear">{empty.cta}</button>
      {:else}
        <a class="btn-secondary" href="/rate">{empty.cta}</a>
      {/if}
    </div>
  {/if}

  <!-- Empty tiers stay on screen as drop targets. -->
  <div class="board" data-testid="rank-board">
    {#each rank.tiers as tier (tier.index)}
      <div
        class="tier"
        role="group"
        aria-labelledby={`tier-${tier.index}`}
        data-tier={tier.label}
        data-tier-index={tier.index}
        ondragover={(e) => e.preventDefault()}
        ondrop={(e) => onDropInto(tier.index, e)}
      >
        <div class="tier-head" data-testid={`rank-tier-${tier.label}`}>
          <h2 class="tier-name" id={`tier-${tier.index}`}>
            <span class="letter" data-testid={`rank-letter-${tier.label}`}>{tier.label}</span>
            {#if tier.verdict}<span>{tier.verdict}</span>{/if}
          </h2>
          <span class="footnote data">{tier.entries.length}</span>
        </div>
        {#if tier.entries.length}
          <ul class="rows">
            {#each tier.entries as entry (entry.title_id)}
              {@const chip = chipFor(entry)}
              {@const meta = metaOf(entry)}
              <li
                class="row"
                draggable="true"
                ondragstart={(e) => onDragStart(entry, e)}
                ondragend={() => (dragging = null)}
                ondragover={(e) => e.preventDefault()}
                ondrop={(e) => onDropInto(tier.index, e, entry.title_id)}
                data-title={entry.title_id}
                data-testid={`rank-title-${entry.title_id}`}
              >
                <div class="main">
                  <button
                    class="open"
                    onclick={() => openTitle(entry)}
                    data-testid={`rank-open-${entry.title_id}`}
                  >
                    <span class="thumb" aria-hidden="true">
                      <RatePoster title={{ id: entry.title_id, name: entry.name }} showName={false} />
                    </span>
                    <span class="text">
                      <span class="name">{entry.name}</span>
                      {#if meta}<span class="footnote data">{meta}</span>{/if}
                    </span>
                  </button>
                  <button
                    class="move"
                    onclick={() => (moving = entry)}
                    disabled={rank.busy}
                    aria-haspopup="dialog"
                    aria-label={`Move ${entry.name}`}
                    data-testid={`rank-move-${entry.title_id}`}>Move</button
                  >
                </div>
                {#if chip}
                  <!-- Its own control: it opens the comparison queue and moves nothing (decision 496). -->
                  <button
                    class="pill chip"
                    class:tension={chip.kind === 'tension'}
                    onclick={openQueue}
                    disabled={rank.busy}
                    data-chip={chip.kind}
                    data-testid={`rank-chip-${entry.title_id}`}
                    aria-label={`${chip.text} Compare titles to settle it`}>{chip.text}</button
                  >
                {/if}
              </li>
            {/each}
          </ul>
        {:else}
          <p class="nothing">Nothing here yet</p>
        {/if}
      </div>
    {/each}
  </div>

  {#if showModel && rank.model}
    <div class="card data model" data-testid="rank-model">
      cutpoints [{rank.model.cutpoints.map((c) => c.toFixed(2)).join(' · ')}] · straddle_z
      {rank.model.straddle_z} · tension {rank.model.tension_credible_mass} · held-out
      {rank.model.held_out.pairs} pairs
      {#if rank.model.held_out.rate !== null}· agreement {rank.model.held_out.rate.toFixed(2)}{/if}
    </div>
  {/if}
  {#if showModel && rank.log.length}
    <ul class="card data log" data-testid="rank-log">
      {#each rank.log as line}<li>{line}</li>{/each}
    </ul>
  {/if}
</section>

<!-- The bars sit in the content, not the header: the header's grab area captures the pointer. -->
<Sheet open={filtersOpen} onClose={() => (filtersOpen = false)} label="Filters" detent="medium" width={480}>
  {#snippet children(close)}
    <div class="bar">
      <h2 class="section-title">Filters</h2>
      <button class="btn-plain" onclick={close}>Done</button>
    </div>
    <div class="filters" data-testid="rank-filters-sheet">
      <div class="list-group">
        <label class="list-row">
          <span>Genre</span>
          <select bind:value={draft.genre} onchange={() => load(rank.kind)} data-testid="rank-genre">
            <option value="">Any genre</option>
            {#each facets.genres as g (g)}<option value={g}>{g}</option>{/each}
          </select>
        </label>
        <label class="list-row">
          <span>Decade</span>
          <select bind:value={draft.decade} onchange={() => load(rank.kind)} data-testid="rank-decade">
            <option value="">Any decade</option>
            {#each facets.decades as d (d)}<option value={d}>{d}s</option>{/each}
          </select>
        </label>
        <label class="list-row">
          <span>Seen</span>
          <select bind:value={draft.seen} onchange={() => load(rank.kind)} data-testid="rank-seen">
            <option value="any">Seen or not</option>
            <option value="seen">Seen</option>
            <option value="unseen">Not seen</option>
          </select>
        </label>
        <label class="list-row">
          <span>Max length</span>
          <input
            class="value"
            type="number"
            min="1"
            inputmode="numeric"
            placeholder="Any"
            bind:value={draft.runtime_max}
            oninput={typed}
            data-testid="rank-runtime"
          />
          <span class="unit">min</span>
        </label>
        <label class="list-row">
          <span>Taste tag</span>
          <input
            class="value"
            type="text"
            placeholder="e.g. cosy"
            bind:value={draft.dna}
            oninput={typed}
            data-testid="rank-dna"
          />
        </label>
      </div>
      {#if chips.length}
        <button class="btn-plain clear" onclick={clearFilters}>Clear all</button>
      {/if}
    </div>
  {/snippet}
</Sheet>

<Sheet open={rank.queueOpen} onClose={closeQueue} label="Sharpen your list" width={480}>
  {#snippet children(close)}
    <div class="bar">
      <span class="footnote data" data-testid="rank-round">{roundLine()}</span>
      <button class="btn-plain" onclick={close} data-testid="rank-queue-close">Done</button>
    </div>
    <div class="queue" data-testid="rank-queue">
      {#if rank.notice}<p class="footnote" role="status">{rank.notice}</p>{/if}
      {#if rank.roundDone}
        <div class="round-end" data-testid="rank-round-end">
          <h2 class="title-1">{ROUND_END_TITLE}</h2>
          <p class="why">{ROUND_END_TEXT}</p>
          <div class="round-actions">
            <button class="btn-primary" onclick={keepGoing} data-testid="rank-round-more">Keep going</button>
            <button class="btn-secondary" onclick={close} data-testid="rank-round-stop">Done</button>
          </div>
        </div>
      {:else if rank.pair}
        <p class="why reason" data-testid="rank-pair-reason">{rank.pair.reason}</p>
        {#if showModel && rank.pair.model}
          <p class="data arm" data-testid="rank-pair-arm">
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
            <RatePoster title={{ id: rank.pair.title_a, name: rank.pair.name_a }} showName={false} />
            <span class="side-name">{rank.pair.name_a}</span>
          </button>
          <button
            class="side"
            onclick={() => answer('B')}
            disabled={rank.busy}
            aria-label={`Pick ${rank.pair.name_b}`}
            data-testid="rank-pair-b"
          >
            <RatePoster title={{ id: rank.pair.title_b, name: rank.pair.name_b }} showName={false} />
            <span class="side-name">{rank.pair.name_b}</span>
          </button>
        </div>
        <button
          class="btn-secondary tie"
          onclick={() => answer('TIE')}
          disabled={rank.busy}
          data-testid="rank-pair-tie">About the same</button
        >
      {:else}
        <p class="why" data-testid="rank-queue-empty">{rank.queueReason}</p>
      {/if}
      {#if rank.placed.length}
        <!-- Placement, never "moved", whichever arm drew the pair (§13). -->
        <section class="placed" data-testid="rank-placed">
          <h3 class="list-header">Where they sit now</h3>
          <ul class="list-group">
            {#each rank.placed as spot (spot.title_id)}
              <li class="list-row spot" data-testid={`rank-placed-${spot.title_id}`}>
                <span>{spot.name}</span>
                <span class="footnote">{spot.badge}</span>
              </li>
            {/each}
          </ul>
        </section>
      {/if}
    </div>
  {/snippet}
</Sheet>

<ActionSheet
  open={moving !== null}
  title={moving ? `Move ${moving.name}` : ''}
  options={moveOptions}
  onClose={() => (moving = null)}
/>

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
  .rank {
    display: flex;
    flex-direction: column;
    gap: 24px;
    max-width: 720px;
  }
  .stack {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .count,
  .hint {
    margin: 0;
    padding: 0 4px;
    font-variant-numeric: tabular-nums;
  }
  /* Only a mouse can drag; a finger has Move. */
  .hint {
    display: none;
  }
  @media (pointer: fine) {
    .hint {
      display: block;
    }
  }

  .searchrow {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .search {
    position: relative;
    flex: 1;
    min-width: 0;
    display: flex;
    align-items: center;
    color: var(--text-3);
  }
  .search svg {
    position: absolute;
    left: 12px;
    pointer-events: none;
  }
  /* Class and type both: design.css's input rule is four :not()s deep. */
  .search input.q[type='search'] {
    -webkit-appearance: none;
    appearance: none;
    padding-left: 38px;
  }
  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }

  .sharpen {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .sharpen-text {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .card-title {
    margin: 0;
    font-size: var(--fs-body);
    line-height: 22px;
    font-weight: 600;
  }
  .sharpen .why {
    margin: 0;
  }
  .start {
    flex: none;
    min-height: 34px;
    padding: 0 14px;
    border-radius: var(--r-pill);
    font-size: var(--fs-subhead);
    line-height: 20px;
  }

  .error {
    margin: 0;
    color: var(--negative);
    font-size: var(--fs-subhead);
  }
  .empty {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 12px;
  }
  .empty .why {
    margin: 0;
  }

  .board {
    display: flex;
    flex-direction: column;
    gap: 32px;
  }
  .tier {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .tier-head {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .tier-name {
    flex: 1;
    min-width: 0;
    margin: 0;
    display: flex;
    align-items: center;
    gap: 12px;
    font-size: var(--fs-footnote);
    line-height: 18px;
    font-weight: 400;
    color: var(--text-3);
  }
  .letter {
    width: 40px;
    height: 40px;
    flex: none;
    display: grid;
    place-items: center;
    border-radius: var(--r-sm);
    background: var(--surface-2);
    font-family: var(--serif);
    font-size: var(--fs-title);
    line-height: 34px;
    color: var(--text);
  }
  .rows {
    margin: 0;
    padding: 0;
    list-style: none;
    border-radius: var(--r-md);
    background: var(--surface-1);
    overflow: hidden;
  }
  .row {
    position: relative;
  }
  .row + .row::before {
    content: '';
    position: absolute;
    top: 0;
    left: 68px;
    right: 0;
    height: 0.5px;
    background: var(--separator);
  }
  .main {
    display: flex;
    align-items: stretch;
  }
  .open {
    flex: 1;
    min-width: 0;
    min-height: 76px;
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 8px 0 8px 16px;
    border: none;
    background: none;
    color: inherit;
    text-align: left;
  }
  .thumb {
    width: 40px;
    flex: none;
  }
  .text {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .name {
    font-size: var(--fs-body);
    line-height: 22px;
  }
  .move {
    flex: none;
    padding: 0 16px;
    border: none;
    background: none;
    color: var(--text-2);
    font-size: var(--fs-subhead);
  }
  .move:disabled,
  .chip:disabled {
    opacity: 0.45;
  }
  .chip {
    max-width: calc(100% - 84px);
    margin: -4px 16px 12px 68px;
  }
  /* The tension chip carries a sentence, so it wraps. */
  .chip.tension {
    min-height: 36px;
    padding: 8px 12px;
    border-radius: var(--r-sm);
    white-space: normal;
    text-align: left;
    justify-content: flex-start;
  }
  .nothing {
    margin: 0;
    min-height: 52px;
    display: flex;
    align-items: center;
    padding: 0 16px;
    border-radius: var(--r-md);
    background: var(--surface-1);
    font-size: var(--fs-subhead);
    line-height: 20px;
    color: var(--text-3);
  }
  .model,
  .log {
    margin: 0;
  }
  .log {
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    min-height: 44px;
  }
  .bar .btn-plain {
    margin-right: -8px;
    font-weight: 600;
  }

  .filters {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 12px;
    padding-top: 8px;
  }
  .filters .list-group {
    align-self: stretch;
  }
  .filters .list-row > span:first-child {
    flex: none;
  }
  .filters .list-row select,
  .filters .list-row input.value {
    flex: 1;
    min-width: 0;
    width: auto;
    padding: 0;
    background-color: transparent;
    color: var(--text-2);
    text-align: right;
    text-align-last: right;
  }
  .filters .list-row select {
    padding-right: 22px;
    background-position: right 0 center;
  }
  .unit {
    color: var(--text-3);
  }
  .clear {
    margin-left: 8px;
  }

  .queue {
    display: flex;
    flex-direction: column;
    gap: 20px;
    padding-top: 8px;
  }
  .queue p {
    margin: 0;
  }
  .reason,
  .arm {
    text-align: center;
  }
  .pair {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
  }
  .side {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 0;
    border: none;
    background: none;
    color: inherit;
    text-align: center;
  }
  .side-name {
    font-size: var(--fs-body);
    line-height: 22px;
    font-weight: 600;
  }
  .tie {
    width: 100%;
    min-height: 50px;
  }
  .round-end {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 8px;
    padding-top: 24px;
    text-align: center;
  }
  .round-actions {
    align-self: stretch;
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin-top: 24px;
  }
  .round-actions button {
    width: 100%;
    min-height: 50px;
  }
  .placed ul {
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .spot {
    flex-direction: column;
    align-items: flex-start;
    gap: 2px;
  }
</style>
