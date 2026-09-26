<script>
  // One kind's section of a shelf, so a Films row and a Series row never see each other's items
  // (§4.1 rule 5). `sectionShips` has already refused a section without a why-line.
  import PosterCard, { isColdPlaced } from '$lib/components/PosterCard.svelte';
  import ModelNote from '$lib/components/ModelNote.svelte';
  import { facetColour, toPosterTitle, whyNumbersLine } from '$lib/home.svelte.js';
  import { termLabel } from '$lib/terms.js';

  let { section, shelfId, ranking = true, onSelect } = $props();

  // Present only with Show the model on: the server sends `why_numbers` then and at no other time.
  const numbers = $derived(whyNumbersLine(section.why_numbers));

  // "as on your Rank board" only where the server reports the title on the board at this letter;
  // a title watched but never rated is on no board, so `seen` alone cannot decide it.
  function tierName(item) {
    if (item.on_board) {
      return item.board_tier && item.board_tier !== item.tier
        ? `tier ${item.tier}, where your other answers point — you put it in ${item.board_tier} on your Rank board`
        : `tier ${item.tier}, as on your Rank board`;
    }
    return item.seen
      ? `our guess: tier ${item.tier} if you rated it`
      : `our guess: tier ${item.tier} if you rated it — you haven't seen it`;
  }

  // The chip's why, said once for the row: a title= tooltip does not exist on a phone.
  const anyColdPlaced = $derived(section.items.some((item) => isColdPlaced(toPosterTitle(item))));
  // That shelf's why-line already says it.
  const coldNote = $derived(anyColdPlaced && shelfId !== 'new_in_library');

  /** @type {HTMLElement | undefined} */
  let row = $state();
  let atStart = $state(true);
  let atEnd = $state(false);

  function measure() {
    if (!row) return;
    atStart = row.scrollLeft <= 1;
    atEnd = row.scrollLeft + row.clientWidth >= row.scrollWidth - 1;
  }

  $effect(() => {
    // A shorter row has no overflow and must not offer a dead chevron.
    section.items.length;
    measure();
  });

  // Attached by hand: Svelte 5 registers `wheel` as passive, so `preventDefault` in `onwheel`
  // is ignored and the page scrolls along with the row.
  $effect(() => {
    const node = row;
    if (!node) return;
    const onWheel = (event) => {
      if (Math.abs(event.deltaY) <= Math.abs(event.deltaX)) return;
      if (node.scrollWidth <= node.clientWidth) return;
      event.preventDefault();
      node.scrollLeft += event.deltaY;
      measure();
    };
    node.addEventListener('wheel', onWheel, { passive: false });
    return () => node.removeEventListener('wheel', onWheel);
  });

  // Pages 80% of the viewport; scroll-snap lands it on a card.
  function nudge(direction) {
    if (!row) return;
    row.scrollLeft += direction * row.clientWidth * 0.8;
    measure();
  }
</script>

<section
  class="shelf"
  data-testid="shelf"
  data-shelf={shelfId}
  data-kind={section.kind}
  data-ranking={ranking}
>
  <header>
    <div class="titleline">
      <h2 data-testid="shelf-title">{section.title}</h2>
      <span class="kindhead data" data-testid="shelf-kind">{section.heading}</span>
      <!-- A phone shows under three of up to twelve, with no scrollbar: the count says there is more. -->
      <span class="count data" data-testid="shelf-count">
        {section.items.length} {section.items.length === 1 ? 'title' : 'titles'}
      </span>
    </div>
    <p class="why" data-testid="shelf-why">{section.why}</p>
    {#if section.caption}
      <p class="why caption" data-testid="shelf-caption">{section.caption}</p>
    {/if}
    {#if numbers}
      <p class="note data" data-model-note data-testid="shelf-numbers">{numbers}</p>
    {/if}
    {#if coldNote}
      <p class="why" data-testid="shelf-cold-note">
        Cards marked "new" have no outside ratings yet — we placed them by what they're about.
      </p>
    {/if}
    {#if section.shared_terms?.length}
      <!-- The server intersects over the cards returned, so a chip is true of every card here. -->
      <div class="terms">
        <!-- Keyed on facet and term: parallel extraction can put one term on the row twice. -->
        {#each section.shared_terms as t (t.facet + ':' + t.term)}
          <span
            class="term"
            data-testid="shelf-term"
            style:color={facetColour(t.facet)}
            style:border-color={facetColour(t.facet)}
          >{termLabel(t)}{#if t.tier === 'projected'}<span class="tier-note"> ·&nbsp;inferred</span>{/if}</span>
        {/each}
      </div>
    {/if}
  </header>

  <div class="rowwrap" class:start={atStart} class:end={atEnd}>
    <button
      class="nudge left"
      aria-label="Scroll {section.title} left"
      data-testid="shelf-page-left"
      onclick={() => nudge(-1)}
      hidden={atStart}
    >‹</button>

    <div class="row" bind:this={row} onscroll={measure} data-nobar data-testid="shelf-items">
      {#each section.items as item (item.title_id)}
        <div class="cell" data-testid="shelf-card" data-title={item.title_id}>
          <!-- Rank and tier sit under the art: over it they covered the title lettering. -->
          {#snippet chrome()}
            <span class="rank data" data-testid="shelf-rank">{item.rank}</span>
            {#if item.tier}
              <!-- Dashed when it is a guess, solid when the title is on the Rank board. -->
              <span
                class="tierbadge data"
                class:guess={!item.on_board}
                data-testid="shelf-tier"
                data-guess={!item.on_board}
                role="img"
                aria-label={tierName(item)}
                title={tierName(item)}
              >{item.tier}</span>
            {/if}
          {/snippet}
          <PosterCard title={toPosterTitle(item)} onSelect={() => onSelect?.(item.title_id)} {chrome} />
          <ModelNote model={item.model} compact />
        </div>
      {/each}
    </div>

    <button
      class="nudge right"
      aria-label="Scroll {section.title} right"
      data-testid="shelf-page-right"
      onclick={() => nudge(1)}
      hidden={atEnd}
    >›</button>
  </div>
</section>

<style>
  .shelf {
    margin-bottom: 26px;
  }
  header {
    margin-bottom: 9px;
  }
  .titleline {
    display: flex;
    align-items: baseline;
    gap: 10px;
    flex-wrap: wrap;
  }
  h2 {
    margin: 0;
    font-size: 15.5px;
    font-weight: 600;
  }
  .kindhead {
    letter-spacing: 0.1em;
    text-transform: uppercase;
    border: 1px solid var(--line-2);
    border-radius: var(--r-pill);
    padding: 2px 8px;
    color: var(--ink-3);
  }
  .why {
    margin: 4px 0 0;
  }
  /* `.caption` deliberately has no rule of its own: §6.8's quiet register is one thing. */
  .terms {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    margin-top: 6px;
  }
  .term {
    font-family: var(--mono);
    font-size: 10px;
    padding: 2px 7px;
    border: 1px solid;
    border-radius: var(--r-pill);
    opacity: 0.9;
  }
  .tier-note {
    opacity: 0.65;
  }

  .rowwrap {
    position: relative;
  }
  .row {
    display: grid;
    grid-auto-flow: column;
    grid-auto-columns: 132px;
    gap: 14px;
    overflow-x: auto;
    overflow-y: hidden;
    /* No smooth scrolling: a scripted Chrome does not animate it, so a chevron could not be tested. */
    scroll-snap-type: x proximity;
    padding-bottom: 2px;
  }
  .cell {
    /* A grid, not a block: a <button> in flow shrinks to fit, so short titles made the row ragged. */
    display: grid;
    align-content: start;
    position: relative;
    scroll-snap-align: start;
    min-width: 0;
  }
  .rank,
  .tierbadge {
    padding: 1px 6px;
    border-radius: var(--r-pill);
    background: var(--card);
    border: 1px solid var(--line);
    font-size: 9px;
    color: var(--ink-3);
    pointer-events: none;
  }
  .tierbadge {
    color: var(--ink-2);
  }
  .tierbadge.guess {
    border-style: dashed;
    border-color: var(--line-2);
    color: var(--ink-3);
  }
  .count {
    color: var(--ink-4);
    letter-spacing: 0.04em;
  }
  .note {
    margin: 4px 0 0;
    color: var(--ink-4);
  }

  .nudge {
    position: absolute;
    top: 0;
    bottom: 26px;
    width: 42px;
    border: none;
    color: var(--ink-2);
    font-size: 20px;
    cursor: pointer;
    opacity: 0;
    transition: opacity 0.12s ease;
    z-index: 2;
  }
  .nudge.left {
    left: -6px;
    background: linear-gradient(90deg, var(--ground) 40%, rgba(13, 13, 15, 0));
  }
  .nudge.right {
    right: -6px;
    background: linear-gradient(270deg, var(--ground) 40%, rgba(13, 13, 15, 0));
  }
  .rowwrap:hover .nudge:not([hidden]) {
    opacity: 1;
  }
  /* The chevron is `opacity: 0` until hover, so focus must reveal it; the outline restates
     design.css's ring, since this scoped rule would otherwise win on width. */
  .nudge:focus-visible {
    opacity: 1;
    outline: 2px solid var(--ember);
  }

  /* Touch: native momentum scroll and an edge fade instead of chevrons (proposal 28). */
  @media (pointer: coarse) {
    .nudge {
      display: none;
    }
    .row {
      grid-auto-columns: 104px;
      gap: 10px;
    }
    .rowwrap:not(.end)::after {
      content: '';
      position: absolute;
      top: 0;
      right: 0;
      bottom: 26px;
      width: 34px;
      pointer-events: none;
      background: linear-gradient(90deg, rgba(13, 13, 15, 0), var(--ground));
    }
  }
</style>
