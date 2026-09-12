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
  /* How far through the block, in three volumes of one neutral: the remainder is the line the
     UI is already drawn with, the part behind you is ink at reading weight, and the slot you are
     on is full ink. Progress is a fact about the sitting and never a selection, so §6.8's one
     accent does not pay for it — the ramp's brightness carries what the hue used to, and the
     fifteen ticks stop competing with whichever pair is actually being chosen between.
     [§6.8; decision 276] */
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
