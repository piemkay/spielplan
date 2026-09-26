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
  import PosterCard, { isColdPlaced } from '$lib/components/PosterCard.svelte';
  import ModelNote from '$lib/components/ModelNote.svelte';
  import { facetColour, toPosterTitle, whyNumbersLine } from '$lib/home.svelte.js';
  import { termLabel } from '$lib/terms.js';

  let { section, shelfId, ranking = true, onSelect } = $props();

  /** The numbers the shelf's ordering used, present only with Show the model on (decision 486:
   *  the server sends `why_numbers` then and at no other time). */
  const numbers = $derived(whyNumbersLine(section.why_numbers));

  /**
   * The tier letter's own sentence (decision 476). The letter is the model's fitted tier (decision
   * 187); the sentence says what that letter is to this person, and only what holds (decision 486
   * clause 7). "As on your Rank board" is said of a title the server reports ON the board whose
   * board letter is this one. A title the person moved on Rank to a tier the fit does not share
   * says both, in Rank's own words for that disagreement. Every other letter is our guess - a title
   * marked watched but never rated is on no Rank board either, so `seen` alone could not decide it.
   */
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

  /**
   * Does any card on this row wear §8 stage 10's chip?
   *
   * The chip's own explanation lived in a `title=` attribute, which is a hover tooltip and does
   * not exist on the form factor §6's preamble makes primary — so on a phone the badge was an
   * unexplained word on a poster. §6.8's rule is that every conflict carries its one-line why,
   * and the shelf is where that line costs once rather than once per card: twelve identical
   * sentences down a row would be the noise the quiet-reason register exists to avoid.
   * [§6.8; M4.15 finding 10, decision 278]
   */
  const anyColdPlaced = $derived(section.items.some((item) => isColdPlaced(toPosterTitle(item))));
  // "New in the library" IS that sentence: its §6.0 why-line is §8 stage 10's reason, and the row
  // printed it twice, once as the why-line and once as this note (second household test, U10).
  // Every other row still says it once when a card on it wears the chip (decision 278).
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
      <!-- The row holds up to twelve and a phone shows under three, with the scrollbar hidden and
           no chevrons on touch: the count says there is more before the finger has to find out.
           [owner instruction of 2026-09-25] -->
      <span class="count data" data-testid="shelf-count">
        {section.items.length} {section.items.length === 1 ? 'title' : 'titles'}
      </span>
    </div>
    <!-- §6.0's mandatory one-line why, in vocabulary terms (§6.8's "quiet reasons"). -->
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
      <!-- Computed by the server as the intersection over the cards actually returned, so the
           chips cannot be false of a card on this row. -->
      <div class="terms">
        <!-- Keyed on facet AND term, delimited, like the platform-scores block on the title
             card: `dna_tag` is unique on (title_id, version, term, provider), so §6.6's parallel
             extraction mode puts one term on the row twice and an unkeyed duplicate throws
             `each_key_duplicate` in the production build. [M4.9 finding 8]

             The chip prints the term's NAME - "love & romance", not `themes.love_romance` - which
             the payload carries as `label` from the vocabulary the corpus ships (decision 486);
             `termLabel` falls back to the id's leaf in plain words for a payload without one.
             The facet is spent on the colour, which is what §6.8 calls identity rather than
             decoration. -->
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
          <!-- Proposal 29: rank, seen and the settled tier are chrome and stay ungated. The top
               corners of the art belong to M0's own badges (new / seen); rank and tier sit in a
               row under the art, rank at the left and tier at the right, because pinned over the
               poster they covered its title lettering (second household test, U5). §6.3's
               straddle and tension badges deliberately do not appear: Home shows the settled tier
               only. -->
          {#snippet chrome()}
            <span class="rank data" data-testid="shelf-rank">{item.rank}</span>
            {#if item.tier}
              <!-- Dashed when it is a guess (decision 187 badges the fitted tier, which exists for
                   unrated titles too), solid when the title is on the person's Rank board; the
                   name says which for anyone who cannot see the difference, and `ShelfList` says
                   it once in words. -->
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
  /* `.caption` has no rule of its own: it was a second, dimmer colour for a line already in the
     quiet-reason register — --ink-5, the darkest text in the app at 2.22:1 before decision 275
     raised the token — and §6.8's register is one thing or it is not a register. The class stays
     in the markup because `shelf-caption` is what the suite reads. [§6.8; decision 275] */
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
  /* In the row under the art (`PosterCard`'s `chrome`), in flow: it was absolutely placed 30 px
     down the poster and covered the title lettering (second household test, U5). */
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
  /* The guess is told by its dashed edge and dimmer letter, off the art as on it (decision 483). */
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
  /* The accent stays here because a focus ring IS §6.8's other selection: the keyboard has
     chosen this control. The rule does a second job the app's one global ring cannot — the
     chevron is `opacity: 0` until the row is hovered, so without this a keyboard user tabs onto
     a control that is not drawn at all. That reveal is the whole of what the exception buys: the
     outline beside it RESTATES design.css's one ring and does not redefine it, so it is that
     rule's value to the character. It was `1px` here, which made the app's single focus ring two
     of them — 2px everywhere and 1px on this chevron, because a scoped `.nudge:focus-visible`
     outranks a bare `:focus-visible` and wins on width, style and colour while the offset falls
     through. [§6.8; proposals 127, 131; decision 276 keep-list; review cycle 1] */
  .nudge:focus-visible {
    opacity: 1;
    outline: 2px solid var(--ember);
  }

  /* Touch gets native momentum scroll and an edge fade — no chevrons (proposal 28). The fade was
     promised here and never drawn: `.start`/`.end` were toggled on `.rowwrap` with no rule reading
     them, so on a phone a row of twelve read as three cards and an edge. It reads `atEnd`, which
     is already measured, and it is `pointer-events: none` so it never takes a tap. Columns are the
     catalog grid's phone minimum, so three whole cards and a peek of the fourth fit on an iPhone
     13 instead of two and a clipped third. [owner instruction of 2026-09-25] */
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
