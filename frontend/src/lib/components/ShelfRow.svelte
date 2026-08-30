<script>
  /**
   * One shelf section — that is, ONE kind's half of one §6.0 shelf. Spec v2.1 §6.0 (M2),
   * §4.1 rule 5, §6.8; decision 18; proposals 28 and 29.
   *
   * WHY THIS COMPONENT IS A SECTION AND NOT A SHELF. Decision 18's reading of rule 5: "a
   * surface that **ranks** … renders two headed sections and never one interleaved ranking".
   * The payload has no shelf-level `items` to interleave, and neither does this component: it
   * is handed one section and can only render that section's own ordering, so a Films row and
   * a Series row are two instances that never see each other's arrays.
   *
   * §6.0 M2: "a shelf that cannot say why it exists doesn't ship." The why-line is not
   * optional chrome below the title — `sectionShips` refuses the section upstream, and the
   * markup here renders the why-line unconditionally because there is no state in which it is
   * absent.
   *
   * Proposal 28's interaction contract: "Pointer devices get wheel-to-horizontal with axis
   * detection plus hover-revealed edge chevrons paging 80% of the viewport; touch gets native
   * momentum scroll with an edge-fade affordance and no chevrons."
   */
  import PosterCard from '$lib/components/PosterCard.svelte';
  import ModelNote from '$lib/components/ModelNote.svelte';
  import { facetColour, toPosterTitle } from '$lib/home.svelte.js';

  let { section, shelfId, ranking = true, onSelect } = $props();

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
    // Re-measure when the items change: a shorter row has no overflow and must not offer a
    // chevron that does nothing.
    section.items.length;
    measure();
  });

  /**
   * Wheel-to-horizontal with axis detection. `preventDefault` is the half the prototype
   * omitted — without it the row scrolls sideways AND the page scrolls down, which reads as
   * the page jumping away under the finger.
   *
   * Attached by hand rather than with `onwheel={…}`, because Svelte 5 registers `wheel`,
   * `touchstart` and `touchmove` as PASSIVE listeners: `preventDefault` inside one is ignored,
   * so the markup form silently kept the page scrolling. Measured, not assumed — with
   * `onwheel` the page moved 400 → 700 px and the row never moved at all.
   */
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

  /**
   * Proposal 28's "paging 80% of the viewport". Scroll-snap lands it on a card boundary; see
   * the note on `.row` for why there is no smooth animation.
   */
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
      <!-- §4.1 rule 5 made visible: the kind heading rides on the row, not on the page, so a
           Films row and a Series row of the same shelf are legibly two rankings. -->
      <span class="kindhead data" data-testid="shelf-kind">{section.heading}</span>
    </div>
    <!-- §6.0's mandatory one-line why, in vocabulary terms (§6.8's "quiet reasons"). -->
    <p class="why" data-testid="shelf-why">{section.why}</p>
    {#if section.caption}
      <p class="why caption" data-testid="shelf-caption">{section.caption}</p>
    {/if}
    {#if section.shared_terms?.length}
      <!-- Computed by the server as the intersection over the cards actually returned, so the
           chips cannot be false of a card on this row. -->
      <div class="terms">
        {#each section.shared_terms as t (t.term)}
          <span
            class="term"
            data-testid="shelf-term"
            style:color={facetColour(t.facet)}
            style:border-color={facetColour(t.facet)}
          >{t.facet}.{t.term}{#if t.tier === 'projected'}<span class="tier-note"> ·&nbsp;projected</span>{/if}</span>
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
          <PosterCard title={toPosterTitle(item)} onSelect={() => onSelect?.(item.title_id)} />
          <!-- Proposal 29: rank, seen and the settled tier are chrome and stay ungated. The
               top corners belong to M0's own badges (new / seen), so rank and tier take the
               bottom two — same three overlays, no collision. §6.3's straddle and tension
               badges deliberately do not appear: Home shows the settled tier only. -->
          <span class="rank data" data-testid="shelf-rank">{item.rank}</span>
          {#if item.tier}<span class="tierbadge data" data-testid="shelf-tier">{item.tier}</span>{/if}
          <!-- Present only when decision 117's toggle is on; the server strips the block. -->
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
  .caption {
    color: var(--ink-5);
  }
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
    /* The row scrolls, never the page: §6 preamble's phone-first shell must not gain a
       horizontal scrollbar because a shelf is twelve cards wide. */
    overflow-x: auto;
    overflow-y: hidden;
    /* Snap, but not `scroll-behavior: smooth`. Measured on this machine: a scripted Chrome
       does not animate a smooth scroll at all — `scrollBy({behavior:'smooth'})` and an
       assignment under `scroll-behavior: smooth` both leave `scrollLeft` at 0, while the
       default behaviour lands on the next card. A chevron that pages only for humans is a
       chevron nobody can test, so the animation goes and the snap stays. */
    scroll-snap-type: x proximity;
    padding-bottom: 2px;
  }
  .cell {
    /* A grid, not a block: `PosterCard` is a `<button>`, and a button in normal flow takes a
       shrink-to-fit width — so a card whose title is one short word came out narrower than
       the track and the row looked ragged. As a grid item it stretches, exactly as it does in
       the catalog's own `.grid`. */
    display: grid;
    align-content: start;
    position: relative;
    scroll-snap-align: start;
    min-width: 0;
  }
  .rank,
  .tierbadge {
    /* Offset from the TOP, not the bottom: the card's meta block is one or two lines tall
       depending on the title, so a bottom offset drifts off the poster on half the row. 30px
       clears M0's own `new` / `seen` badges, which sit at 7px. */
    position: absolute;
    top: 30px;
    padding: 2px 6px;
    border-radius: var(--r-pill);
    background: rgba(13, 13, 15, 0.72);
    border: 1px solid var(--line);
    font-size: 9px;
    color: var(--ink-3);
    pointer-events: none;
  }
  .rank {
    left: 6px;
  }
  .tierbadge {
    right: 6px;
    color: var(--ink-2);
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
  .nudge:focus-visible {
    opacity: 1;
    outline: 1px solid var(--ember);
  }

  /* Touch gets native momentum scroll and an edge fade — no chevrons (proposal 28). */
  @media (pointer: coarse) {
    .nudge {
      display: none;
    }
    .row {
      grid-auto-columns: 116px;
      gap: 10px;
    }
  }
</style>
