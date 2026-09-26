<script>
  import { counterLine } from '$lib/rate.svelte.js';

  let { block, kinds, mode } = $props();

  const line = $derived(counterLine(block, kinds, mode));
  const size = $derived(block?.size ?? 15);
  const slot = $derived(block?.slot ?? 0);
  const ticks = $derived(Array.from({ length: size }, (_, i) => i + 1));
</script>

<div class="counter" data-testid="rate-block">
  <div class="ticks" aria-hidden="true">
    {#each ticks as t (t)}
      <span class="tick" class:done={t < slot} class:now={t === slot}></span>
    {/each}
  </div>
  <span class="data-lg" data-testid="rate-counter">{line}</span>
</div>

<style>
  .counter {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .ticks {
    display: flex;
    gap: 3px;
  }
  /* A neutral ramp, not the accent: progress is never a selection (§6.8). */
  .tick {
    height: 3px;
    flex: 1;
    min-width: 6px;
    border-radius: 2px;
    background: var(--progress-track);
  }
  .tick.done {
    background: var(--progress-fill);
  }
  .tick.now {
    background: var(--progress-now);
  }
  [data-testid='rate-counter'] {
    letter-spacing: 0.04em;
  }
</style>
