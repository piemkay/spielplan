<script>
  // The server's own counter, verbatim: Undo's depth is counted in it (decision 35). `answered`
  // lights the card's dash on the tap; the fifteenth seals the row into one line.
  let { block, answered = false, children = undefined } = $props();

  const size = $derived(block?.size ?? 15);
  const slot = $derived(block?.slot ?? 0);
  const ticks = $derived(Array.from({ length: size }, (_, i) => i + 1));
</script>

<div class="counter" data-testid="rate-block">
  <div class="ticks" aria-hidden="true">
    {#each ticks as t (t)}
      <span class="tick" class:done={t < slot} class:cur={t === slot} class:hit={t === slot && answered}
      ></span>
    {/each}
    {#if answered && slot === size}<span class="seal"></span>{/if}
  </div>
  <span class="count" data-testid="rate-counter">{block?.counter ?? ''}</span>
  {#if children}{@render children()}{/if}
</div>

<style>
  .counter {
    height: 24px;
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .count {
    flex: none;
    font-size: var(--fs-footnote);
    line-height: 18px;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
    color: var(--text-2);
  }
  .ticks {
    position: relative;
    flex: 1;
    min-width: 0;
    height: 4px;
    display: grid;
    grid-auto-flow: column;
    grid-auto-columns: minmax(0, 1fr);
    gap: 3px;
  }
  /* A neutral fill, not the accent: progress is never a selection. The card on screen is faint
     until it is answered; a new one draws in from the left, and a taken-back one drains away. */
  .tick {
    position: relative;
    border-radius: 2px;
    background: var(--progress-track);
  }
  .tick::after {
    content: '';
    position: absolute;
    inset: 0;
    border-radius: inherit;
    background: var(--text);
    opacity: 0.35;
    transform: scaleX(0);
    transform-origin: right center;
    transition: transform 240ms var(--ease), opacity var(--dur-quick) var(--ease);
  }
  .done::after,
  .cur::after {
    transform: none;
  }
  .cur::after {
    transform-origin: left center;
  }
  .done::after,
  .hit::after {
    opacity: 1;
  }
  .hit {
    animation: tick-pop 260ms var(--ease);
  }
  .seal {
    position: absolute;
    inset: 0;
    border-radius: 2px;
    background: var(--text);
    transform-origin: left center;
    animation: grow-x 360ms var(--ease) 120ms both;
  }
  @keyframes tick-pop {
    40% { transform: scaleY(1.75); }
  }
</style>
