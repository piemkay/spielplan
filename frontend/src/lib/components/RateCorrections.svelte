<script>
  let { sides, label, busy = false, onCorrect } = $props();
</script>

<div class="corrections" data-testid="rate-corrections">
  <span class="lead data">{label}:</span>
  {#each sides as side (side)}
    <button
      class="side"
      data-testid="rate-correction-{side}"
      disabled={busy}
      onclick={() => onCorrect(side)}
    >{side}</button>
  {/each}
</div>

<style>
  .corrections {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    padding: 10px 12px;
    border-top: 1px solid var(--line);
  }
  .lead {
    letter-spacing: 0.06em;
  }
  .side {
    padding: 7px 14px;
    min-height: 34px;
    border-radius: var(--r-pill);
    border: 1px solid var(--line-2);
    background: transparent;
    color: var(--ink-3);
    font-family: var(--mono);
    font-size: 11px;
    cursor: pointer;
    transition: border-color 0.12s ease, color 0.12s ease;
  }
  .side:hover:not(:disabled),
  .side:focus-visible {
    border-color: var(--ember);
    color: var(--ink-2);
  }
  .side:disabled {
    opacity: 0.45;
    cursor: default;
  }
  @media (pointer: coarse) {
    .side {
      min-height: var(--touch);
    }
  }
</style>
