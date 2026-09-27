<script>
  // What sits behind the card's "Why these?": the learning curve, and in Pairs and Mixed why the
  // pairs are random and what a clear favourite does (decision 527).
  import {
    DECISIVE_COPY,
    DECISIVE_LABEL,
    LEARNING_CURVE_COPY,
    LEARNING_TARGET,
    PAIR_SELECTION_COPY,
    ratingsLabel
  } from '$lib/rate.svelte.js';

  let { balance, mode, kinds = [], showModel = false } = $props();

  const labelled = $derived(balance?.total ?? 0);
  const position = $derived(Math.min(100, Math.round((labelled / LEARNING_TARGET) * 100)));
</script>

<div class="rail" data-testid="rate-rail">
  <section class="card" data-testid="rate-learning-curve">
    <h3>Where you are</h3>
    <div class="curve" role="img" aria-label="{labelled} of {LEARNING_TARGET} ratings">
      <span class="fill" style:width="{position}%"></span>
      <span class="mark" style:left="50%"></span>
    </div>
    <p class="count" data-testid="rate-label-count">
      {ratingsLabel(labelled, kinds)} · 50–100 gets you started
    </p>
    <p class="why">{LEARNING_CURVE_COPY}</p>
  </section>

  {#if mode !== 'sweep'}
    <section class="card" data-testid="rate-pair-selection">
      <h3>Why these pairs</h3>
      <p class="why">{PAIR_SELECTION_COPY}</p>
    </section>

    <section class="card" data-testid="rate-resolution">
      <h3>{DECISIVE_LABEL}</h3>
      <p class="why">{DECISIVE_COPY}</p>
      {#if showModel}
        <p class="data" data-testid="rate-margin-weights">decisive 1.6 · hesitant 1.0</p>
      {/if}
    </section>
  {/if}
</div>

<style>
  .rail {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .card {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  h3 {
    margin: 0;
    font-size: var(--fs-subhead);
    line-height: 20px;
    font-weight: 600;
  }
  p {
    margin: 0;
  }
  .count {
    font-size: var(--fs-subhead);
    line-height: 20px;
    font-variant-numeric: tabular-nums;
  }
  /* A neutral fill, not the accent: progress is never a selection. */
  .curve {
    position: relative;
    height: 6px;
    border-radius: var(--r-pill);
    background: var(--progress-track);
    overflow: hidden;
  }
  .fill {
    position: absolute;
    inset: 0 auto 0 0;
    background: var(--text);
    border-radius: var(--r-pill);
  }
  .mark {
    position: absolute;
    top: 0;
    bottom: 0;
    width: 1px;
    background: var(--text-3);
  }
</style>
