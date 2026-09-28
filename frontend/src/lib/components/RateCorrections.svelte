<script>
  // §6.1's corrections zone: a side the person has not seen leaves the pair and writes no duel.
  let { sides, names = {}, busy = false, onCorrect } = $props();

  const ORDER = ['left', 'right', 'both'];
  const shown = $derived(ORDER.filter((side) => sides.includes(side)));
  const label = (side) => (side === 'both' ? 'Neither' : names[side] ?? side);
  const spoken = (side) =>
    side === 'both' ? "I haven't seen either" : `I haven't seen ${names[side] ?? side}`;
</script>

<section class="corrections" data-testid="rate-corrections" aria-labelledby="rate-unseen-label">
  <h3 class="footnote" id="rate-unseen-label">Haven't seen one?</h3>
  <div class="row">
    {#each shown as side (side)}
      <button
        class="pill"
        data-testid="rate-correction-{side}"
        aria-label={spoken(side)}
        disabled={busy}
        onclick={() => onCorrect(side)}
      ><span class="name">{label(side)}</span></button>
    {/each}
  </div>
</section>

<style>
  .corrections {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding-top: 12px;
    box-shadow: inset 0 0.5px 0 var(--separator);
  }
  h3 {
    margin: 0;
    font-weight: 400;
  }
  .row {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .pill {
    max-width: 100%;
    font-weight: 600;
  }
  .pill:disabled {
    opacity: 0.45;
    cursor: default;
  }
  .name {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }
</style>
