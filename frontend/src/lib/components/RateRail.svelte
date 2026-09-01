<script>
  /**
   * §6.1's side rail: the measured expectations that make this surface honest about what it is
   * asking for and why.
   *
   * Proposal 43 is the reason it is a component rather than a desktop-only column: "on phones
   * nothing in the rail is dropped … the measured-expectation cards (learning curve, pair
   * selection, resolution) move behind one `why these pairs?` sheet." Same content, one
   * disclosure — not a second, thinner product. The disclosure is a plain button rather than
   * `<details>` so that the wide layout can pin the content open with a media query alone;
   * a `<details>` closed on a phone stays closed when the window grows, and the summary that
   * would reopen it is the thing the wide layout hides.
   *
   * Proposal 49 is why the learning curve carries a number: "plotted against the user's own
   * lifetime label count — the copy is the caption, the position is the point. This is the one
   * place §12's M2 exit criterion (50–100 verdicts each) is legible to the user."
   */
  import {
    DECISIVE_COPY,
    LEARNING_CURVE_COPY,
    LEARNING_TARGET,
    PAIR_SELECTION_COPY
  } from '$lib/rate.svelte.js';

  let { balance, mode } = $props();

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
      <div class="curve" role="img" aria-label="{labelled} of {LEARNING_TARGET} labels">
        <span class="fill" style:width="{position}%"></span>
        <span class="mark" style:left="50%"></span>
      </div>
      <div class="data-lg" data-testid="rate-label-count">
        {labelled} labels · 50–100 is the first sitting or two
      </div>
      <p class="why">{LEARNING_CURVE_COPY}</p>
    </section>

    {#if mode !== 'sweep'}
      <section class="card" data-testid="rate-pair-selection">
        <span class="eyebrow">PAIR SELECTION</span>
        <!-- Proposal 53: the "Random pairs." lead-in turns a defence into a statement. -->
        <p class="why">{PAIR_SELECTION_COPY}</p>
      </section>

      <section class="card" data-testid="rate-resolution">
        <span class="eyebrow">RESOLUTION</span>
        <p class="why">{DECISIVE_COPY} — §6.1 sets the margin weight at ~1.6 against ~1.0.</p>
        <div class="data">decisive 1.6 · hesitant 1.0</div>
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
    /* Three panels stacked in proposal 43's rail column beside the queue. The rail is
       dense by construction; a content-card box would make these the roomiest thing in
       the narrowest column. */
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
  .curve {
    position: relative;
    height: 6px;
    border-radius: 3px;
    background: var(--line-2);
    overflow: hidden;
  }
  .fill {
    position: absolute;
    inset: 0 auto 0 0;
    background: var(--ember);
    border-radius: 3px;
  }
  .mark {
    position: absolute;
    top: -2px;
    bottom: -2px;
    width: 1px;
    background: var(--ink-4);
  }

  /* The wide layout pins the rail open; the disclosure only does work on compact. */
  @media (min-width: 981px) {
    .disclosure {
      display: none;
    }
    .cards {
      display: flex;
    }
  }
</style>
