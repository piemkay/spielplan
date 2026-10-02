<script>
  // One kind's section of a shelf, so a Films row and a Series row never see each other's items
  // (§4.1 rule 5). No rank number and no tier letter: those live on Rank (decision 527).
  import { untrack } from 'svelte';
  import Icon from '$lib/components/Icon.svelte';
  import PosterCard, { isColdPlaced } from '$lib/components/PosterCard.svelte';
  import ModelNote from '$lib/components/ModelNote.svelte';
  import Sheet from '$lib/components/Sheet.svelte';
  import WorthGettingSheet from '$lib/components/WorthGettingSheet.svelte';
  import { toPosterTitle, whyNumbersLine } from '$lib/home.svelte.js';

  // `level` is 'h3' inside a kind region, whose own heading is the h2. `enter` is the row's place
  // in a staggered arrival, read once: a row already drawn never replays it.
  let { section, shelfId, onSelect, level = 'h2', enter = null } = $props();
  const arrival = untrack(() => enter);

  // Present only with Show the model on: the server sends `why_numbers` then and at no other time.
  const numbers = $derived(whyNumbersLine(section.why_numbers));

  // The badge's why, said once for the row: a title= tooltip does not exist on a phone.
  const anyColdPlaced = $derived(section.items.some((item) => isColdPlaced(toPosterTitle(item))));
  // That shelf's why-line already says it.
  const coldNote = $derived(anyColdPlaced && shelfId !== 'new_in_library');
  // Beyond the library: larger cards, each naming the liked film it is like; See all is its own list.
  const worth = $derived(shelfId === 'worth_getting');

  /** @type {HTMLElement | undefined} */
  let row = $state();
  let atStart = $state(true);
  let atEnd = $state(false);
  let seeAll = $state(false);

  function measure() {
    if (!row) return;
    atStart = row.scrollLeft <= 1;
    atEnd = row.scrollLeft + row.clientWidth >= row.scrollWidth - 1;
  }

  $effect(() => {
    // A shorter row has no overflow, and a chevron with nowhere to go is disabled.
    section.items.length;
    measure();
  });

  // Pages 80% of the viewport; scroll-snap lands it on a card.
  function nudge(direction) {
    if (!row) return;
    row.scrollLeft += direction * row.clientWidth * 0.8;
    measure();
  }
</script>

<svelte:window onresize={measure} />

<section
  class="shelf"
  class:enter={arrival !== null}
  style:--i={arrival}
  data-testid="shelf"
  data-shelf={shelfId}
  data-kind={section.kind}
>
  <div class="head">
    <header>
      <svelte:element this={level} class="section-title" data-testid="shelf-title"
        >{section.title}</svelte:element
      >
      <p class="why" data-testid="shelf-why">{section.why}</p>
      {#if section.caption}
        <p class="footnote" data-testid="shelf-caption">{section.caption}</p>
      {/if}
      {#if numbers}
        <p class="data" data-model-note data-testid="shelf-numbers">{numbers}</p>
      {/if}
    </header>
    <!-- The wheel always scrolls the page; a pointer pages a row from here (decision 528). -->
    <div class="paging">
      <button class="btn-plain" data-testid="shelf-see-all" onclick={() => (seeAll = true)}>See all</button>
      <button
        class="step"
        aria-label="Previous page"
        data-testid="shelf-page-left"
        disabled={atStart}
        onclick={() => nudge(-1)}
      ><Icon name="chevron-left" size={18} /></button>
      <button
        class="step"
        aria-label="Next page"
        data-testid="shelf-page-right"
        disabled={atEnd}
        onclick={() => nudge(1)}
      ><Icon name="chevron-right" size={18} /></button>
    </div>
  </div>

  <div class="rowwrap" class:fade-start={!atStart} class:fade-end={!atEnd}>
    <div class="row" class:worth bind:this={row} onscroll={measure} data-nobar data-testid="shelf-items">
      {#each section.items as item (item.title_id)}
        {@const title = toPosterTitle(item)}
        <div class="cell" data-testid="shelf-card" data-title={item.title_id}>
          <PosterCard {title} onSelect={() => onSelect?.(title)} />
          <ModelNote model={item.model} compact />
        </div>
      {/each}
    </div>
  </div>

  {#if coldNote}
    <p class="footnote" data-testid="shelf-cold-note">
      Titles marked New have no outside ratings yet — we placed them by what they're about.
    </p>
  {/if}
</section>

{#if worth}
  <WorthGettingSheet open={seeAll} onClose={() => (seeAll = false)} kind={section.kind} {onSelect} />
{:else}
<Sheet open={seeAll} onClose={() => (seeAll = false)} label={section.title} width={880}>
  {#snippet header(close)}
    <div class="sheet-bar">
      <h2>{section.title}</h2>
      <button class="btn-plain" onclick={close}>Done</button>
    </div>
  {/snippet}
  <div class="all" data-testid="shelf-all">
    {#each section.items as item (item.title_id)}
      {@const title = toPosterTitle(item)}
      <PosterCard {title} onSelect={() => onSelect?.(title)} />
    {/each}
  </div>
</Sheet>
{/if}

<style>
  .shelf {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .head {
    display: flex;
    align-items: flex-start;
    gap: 16px;
  }
  header {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  header p {
    margin: 0;
  }
  .footnote {
    margin: 0;
  }

  .rowwrap {
    position: relative;
    /* The row runs to the screen's edges; a card cut by the edge says there is more. */
    margin: 0 calc(-1 * var(--gutter));
  }
  .row {
    display: grid;
    grid-auto-flow: column;
    grid-auto-columns: 104px;
    gap: 10px;
    padding: 0 var(--gutter);
    scroll-padding-inline: var(--gutter);
    overflow-x: auto;
    overflow-y: hidden;
    overscroll-behavior-x: contain;
    /* No smooth scrolling: a scripted Chrome does not animate it, so a chevron could not be tested. */
    scroll-snap-type: x proximity;
  }
  .row.worth {
    grid-auto-columns: 132px;
    gap: 12px;
  }
  .cell {
    display: grid;
    align-content: start;
    gap: 4px;
    scroll-snap-align: start;
    min-width: 0;
  }

  /* Centred on the title's line, so the controls never push the header taller. */
  .paging {
    flex: none;
    height: 25px;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .paging .btn-plain {
    padding: 0 4px;
    font-size: var(--fs-subhead);
  }
  .step {
    width: 32px;
    height: 32px;
    padding: 0;
    border: none;
    border-radius: var(--r-pill);
    background: var(--surface-2);
    color: var(--text);
    display: grid;
    place-items: center;
  }
  .step:hover:not(:disabled) {
    background: var(--surface-3);
  }
  .step:disabled {
    color: rgba(245, 240, 232, 0.35);
    cursor: default;
  }
  /* Fingers swipe the row. */
  @media (pointer: coarse) {
    .step {
      display: none;
    }
  }
  /* A pointer sees a fade on whichever side has more posters. */
  @media (pointer: fine) {
    .rowwrap::before,
    .rowwrap::after {
      content: '';
      position: absolute;
      top: 0;
      bottom: 0;
      z-index: 1;
      width: 64px;
      pointer-events: none;
      opacity: 0;
      transition: opacity 0.12s var(--ease);
    }
    .rowwrap::before {
      left: 0;
      background: linear-gradient(to right, var(--bg), transparent);
    }
    .rowwrap::after {
      right: 0;
      background: linear-gradient(to left, var(--bg), transparent);
    }
    .fade-start::before,
    .fade-end::after {
      opacity: 1;
    }
  }

  .sheet-bar {
    min-height: 44px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  .sheet-bar h2 {
    margin: 0;
    min-width: 0;
    overflow: hidden;
    white-space: nowrap;
    text-overflow: ellipsis;
    font-size: var(--fs-body);
    line-height: 22px;
    font-weight: 600;
  }
  .all {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 20px 12px;
    padding-top: 8px;
  }

  @media (min-width: 721px) {
    .rowwrap {
      margin: 0;
    }
    .row,
    .row.worth {
      grid-auto-columns: var(--shelf-poster);
      gap: 16px;
      padding: 0;
      scroll-padding-inline: 0;
    }
    .all {
      grid-template-columns: repeat(auto-fill, var(--shelf-poster));
      gap: 24px 16px;
    }
  }
</style>
