<script>
  /**
   * §6.1's block counter — "blocks of 15" — with proposal 46's partition and the card type
   * being served: `7 / 15 this block · film · sweep`.
   *
   * Decision 35 is the reason this is a component and not a string in the header: "the depth
   * matches the counter the user is already reading (\"7 / 15 this block\")". The number here
   * and the number Undo obeys are the same number because both come out of the same response —
   * `session.block.counter` and `undo.available` — so there is nothing for the client to
   * recompute and therefore nothing for it to get wrong.
   *
   * The fifteen ticks are the same claim drawn rather than spelt: the person can see how far
   * back Undo reaches without being told.
   */
  import { counterLine } from '$lib/rate.svelte.js';

  let { block, kinds } = $props();

  const line = $derived(counterLine(block, kinds));
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
  .tick {
    height: 3px;
    flex: 1;
    min-width: 6px;
    border-radius: 2px;
    background: var(--line-2);
  }
  .tick.done {
    background: var(--ember-edge);
  }
  .tick.now {
    background: var(--ember);
  }
  [data-testid='rate-counter'] {
    letter-spacing: 0.04em;
  }
</style>
