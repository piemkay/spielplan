<script>
  // One kind's section of a shelf, so a Films row and a Series row never see each other's items
  // (§4.1 rule 5). No rank number and no tier letter: those live on Rank (decision 527).
  import PosterCard, { isColdPlaced } from '$lib/components/PosterCard.svelte';
  import ModelNote from '$lib/components/ModelNote.svelte';
  import { toPosterTitle, whyNumbersLine } from '$lib/home.svelte.js';

  // `level` is 'h3' inside a kind region, whose own heading is the h2.
  let { section, shelfId, onSelect, level = 'h2' } = $props();

  // Present only with Show the model on: the server sends `why_numbers` then and at no other time.
  const numbers = $derived(whyNumbersLine(section.why_numbers));

  // The badge's why, said once for the row: a title= tooltip does not exist on a phone.
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
>
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

  <div class="rowwrap">
    <button
      class="nudge left"
      aria-label="Scroll {section.title} left"
      data-testid="shelf-page-left"
      onclick={() => nudge(-1)}
      hidden={atStart}
    >
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 5 8 12l7 7" /></svg>
    </button>

    <div class="row" bind:this={row} onscroll={measure} data-nobar data-testid="shelf-items">
      {#each section.items as item (item.title_id)}
        <div class="cell" data-testid="shelf-card" data-title={item.title_id}>
          <PosterCard title={toPosterTitle(item)} onSelect={() => onSelect?.(item.title_id)} />
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
    >
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9.5 5.5 6.5 6.5-6.5 6.5" /></svg>
    </button>
  </div>

  {#if coldNote}
    <p class="footnote" data-testid="shelf-cold-note">
      Titles marked New have no outside ratings yet — we placed them by what they're about.
    </p>
  {/if}
</section>

<style>
  .shelf {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  header {
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
    /* No smooth scrolling: a scripted Chrome does not animate it, so a chevron could not be tested. */
    scroll-snap-type: x proximity;
  }
  .cell {
    display: grid;
    align-content: start;
    gap: 4px;
    scroll-snap-align: start;
    min-width: 0;
  }

  .nudge {
    position: absolute;
    top: 57px;
    z-index: 2;
    width: 40px;
    height: 40px;
    border: none;
    border-radius: var(--r-pill);
    background: var(--surface-3);
    box-shadow: var(--shadow-menu);
    color: var(--text);
    display: grid;
    place-items: center;
    opacity: 0;
    transition: opacity 0.12s var(--ease);
  }
  .nudge[hidden] {
    display: none;
  }
  .nudge.left {
    left: 4px;
  }
  .nudge.right {
    right: 4px;
  }
  .rowwrap:hover .nudge,
  .nudge:focus-visible {
    opacity: 1;
  }

  /* Touch: native momentum scroll instead of chevrons. */
  @media (pointer: coarse) {
    .nudge {
      display: none;
    }
  }

  @media (min-width: 721px) {
    .rowwrap {
      margin: 0;
    }
    .row {
      grid-auto-columns: 148px;
      gap: 16px;
      padding: 0;
      scroll-padding-inline: 0;
    }
    .nudge {
      top: 91px;
    }
  }
</style>
