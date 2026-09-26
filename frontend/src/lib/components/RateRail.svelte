<script>
  // A button, not <details>: the wide layout pins the cards open with a media query alone, and a
  // <details> closed on a phone would stay closed when the window grows.
  import {
    DECISIVE_COPY,
    LEARNING_CURVE_COPY,
    LEARNING_TARGET,
    PAIR_SELECTION_COPY,
    ratingsLabel
  } from '$lib/rate.svelte.js';

  let { balance, mode, kinds = [], showModel = false } = $props();

  let open = $state(false);

  const labelled = $derived(balance?.total ?? 0);
  const position = $derived(Math.min(100, Math.round((labelled / LEARNING_TARGET) * 100)));
</script>

<aside class="rail" data-testid="rate-rail">
  <button
    class="disclosure"
    data-testid="rate-why-pairs"
    aria-expanded={open}
    onclick={() => (open = !open)}
  >{open ? '▾' : '▸'} why these questions?</button>

  <div class="cards" class:open>
    <section class="card" data-testid="rate-learning-curve">
      <span class="eyebrow">WHERE YOU ARE</span>
      <div class="curve" role="img" aria-label="{labelled} of {LEARNING_TARGET} ratings">
        <span class="fill" style:width="{position}%"></span>
        <span class="mark" style:left="50%"></span>
      </div>
      <div class="data-lg" data-testid="rate-label-count">
        {ratingsLabel(labelled, kinds)} · 50-100 gets you started
      </div>
      <p class="why">{LEARNING_CURVE_COPY}</p>
    </section>

    {#if mode !== 'sweep'}
      <section class="card" data-testid="rate-pair-selection">
        <span class="eyebrow">WHY THESE PAIRS</span>
        <p class="why">{PAIR_SELECTION_COPY}</p>
      </section>

      <section class="card" data-testid="rate-resolution">
        <span class="eyebrow">CLEAR FAVOURITES</span>
        <p class="why">{DECISIVE_COPY}</p>
        {#if showModel}
          <div class="data" data-testid="rate-margin-weights">decisive 1.6 · hesitant 1.0</div>
        {/if}
      </section>
    {/if}
  </div>
</aside>

<style>
  .rail {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .disclosure {
    align-self: flex-start;
    background: none;
    border: none;
    padding: 6px 0;
    font-family: var(--mono);
    font-size: 11px;
    color: var(--ink-4);
    cursor: pointer;
  }
  .disclosure:hover {
    color: var(--ink-2);
  }
  .cards {
    display: none;
    flex-direction: column;
    gap: 12px;
  }
  .cards.open {
    display: flex;
  }
  .card {
    display: flex;
    flex-direction: column;
    gap: 7px;
    padding: var(--card-pad-tight);
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: var(--r-md);
  }
  .eyebrow {
    font-family: var(--mono);
    font-size: 9.5px;
    letter-spacing: 0.12em;
    color: var(--ink-4);
  }
  .why {
    margin: 0;
  }
  /* The progress ramp, not the accent: progress is never a selection (§6.8). */
  .curve {
    position: relative;
    height: 6px;
    border-radius: 3px;
    background: var(--progress-track);
    overflow: hidden;
  }
  .fill {
    position: absolute;
    inset: 0 auto 0 0;
    background: var(--progress-fill);
    border-radius: 3px;
  }
  .mark {
    position: absolute;
    top: -2px;
    bottom: -2px;
    width: 1px;
    background: var(--ink-4);
  }

  @media (min-width: 981px) {
    .disclosure {
      display: none;
    }
    .cards {
      display: flex;
    }
  }
</style>
