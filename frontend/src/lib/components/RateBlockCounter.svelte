<script>
  // The server's own counter, verbatim: Undo's depth is counted in it (decision 35).
  let { block, children = undefined } = $props();

  const size = $derived(block?.size ?? 15);
  const slot = $derived(block?.slot ?? 0);
  const ticks = $derived(Array.from({ length: size }, (_, i) => i + 1));
</script>

<div class="counter" data-testid="rate-block">
  <div class="line">
    <span class="count" data-testid="rate-counter">{block?.counter ?? ''}</span>
    {#if children}{@render children()}{/if}
  </div>
  <div class="ticks" aria-hidden="true">
    {#each ticks as t (t)}
      <span class="tick" class:done={t <= slot}></span>
    {/each}
  </div>
</div>

<style>
  .counter {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .line {
    min-height: 18px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  .count {
    font-size: var(--fs-footnote);
    line-height: 18px;
    font-variant-numeric: tabular-nums;
    color: var(--text-2);
  }
  .ticks {
    height: 4px;
    display: grid;
    grid-auto-flow: column;
    grid-auto-columns: minmax(0, 1fr);
    gap: 3px;
  }
  /* A neutral fill, not the accent: progress is never a selection. */
  .tick {
    border-radius: 2px;
    background: var(--progress-track);
  }
  .tick.done {
    background: var(--text);
  }
</style>
