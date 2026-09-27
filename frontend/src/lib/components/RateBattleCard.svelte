<script>
  // No model number before the answer (§6.1's anchoring rule), nothing tappable inside a poster,
  // and Tie is an outcome that writes a duel row. The decisive switch belongs to the pair on the table.
  import { onDestroy } from 'svelte';
  import RateCorrections from '$lib/components/RateCorrections.svelte';
  import RatePoster from '$lib/components/RatePoster.svelte';
  import { DECISIVE_COPY, DECISIVE_LABEL, PAIR_QUESTION, metaLine } from '$lib/rate.svelte.js';

  let {
    card,
    decisive = false,
    busy = false,
    pending = null,
    onDuel,
    onCorrect,
    onSkip,
    onDecisive
  } = $props();

  const left = $derived(card?.left ?? {});
  const right = $derived(card?.right ?? {});

  /** Proposal 51's long-press: "equivalent to toggle-on plus tap", and only that. */
  const LONG_PRESS_MS = 500;
  let timer = null;
  let fired = false;

  // The press names the card it started on: `load()` can swap the card within the 500ms, and a
  // decisive duel on the wrong pair is the heaviest observation there is. `duel()` checks again.
  function press(outcome) {
    fired = false;
    clearTimeout(timer);
    const token = card?.token;
    timer = setTimeout(() => {
      timer = null;
      if (!token || token !== card?.token) return;
      fired = true;
      onDuel(outcome, { decisive: true, token });
    }, LONG_PRESS_MS);
  }

  function release() {
    clearTimeout(timer);
    timer = null;
  }

  // Unarm on teardown, so a pending press cannot write into a card nobody sees.
  onDestroy(release);

  function tap(outcome) {
    release();
    if (fired) {
      // The long press already answered this pair; the click that follows it is the same tap.
      fired = false;
      return;
    }
    onDuel(outcome);
  }
</script>

<article class="battle" data-testid="rate-battle-card" data-card-token={card?.token}>
  <h2 class="question" data-testid="rate-battle-question">{PAIR_QUESTION}</h2>

  <div class="pair">
    <button
      class="side"
      class:picked={pending === `duel-${left.outcome}`}
      data-testid="rate-battle-left"
      aria-label="Pick {left.name ?? 'the left title'}"
      aria-busy={pending === `duel-${left.outcome}`}
      data-outcome={left.outcome}
      data-title-id={left.id}
      disabled={busy}
      onpointerdown={() => press(left.outcome)}
      onpointerup={release}
      onpointerleave={release}
      onpointercancel={release}
      onclick={() => tap(left.outcome)}
    >
      <RatePoster title={left} />
      <span class="data">{metaLine(left)}</span>
    </button>

    <span class="vs data" aria-hidden="true">vs</span>

    <button
      class="side"
      class:picked={pending === `duel-${right.outcome}`}
      data-testid="rate-battle-right"
      aria-label="Pick {right.name ?? 'the right title'}"
      aria-busy={pending === `duel-${right.outcome}`}
      data-outcome={right.outcome}
      data-title-id={right.id}
      disabled={busy}
      onpointerdown={() => press(right.outcome)}
      onpointerup={release}
      onpointerleave={release}
      onpointercancel={release}
      onclick={() => tap(right.outcome)}
    >
      <RatePoster title={right} />
      <span class="data">{metaLine(right)}</span>
    </button>
  </div>

  <p class="why" data-testid="rate-battle-reason">{card?.reason ?? ''}</p>

  <!-- A battle stands in for a sweep only when nothing new is left to rate (§6.1's drained state). -->
  {#if card?.substituted_for}
    <p class="data" data-testid="rate-substituted">
      Nothing new to rate right now - comparing titles you've already rated.
    </p>
  {/if}

  <div class="strip" role="group" aria-label={PAIR_QUESTION}>
    <button
      class="cell"
      class:picked={pending === `duel-${left.outcome}`}
      data-testid="rate-strip-left"
      disabled={busy}
      onclick={() => onDuel(left.outcome)}
    >left</button>
    <button
      class="cell tie"
      class:picked={pending === 'duel-TIE'}
      data-testid="rate-strip-tie"
      aria-busy={pending === 'duel-TIE'}
      disabled={busy}
      onclick={() => onDuel('TIE')}
    >tie</button>
    <button
      class="cell"
      class:picked={pending === `duel-${right.outcome}`}
      data-testid="rate-strip-right"
      disabled={busy}
      onclick={() => onDuel(right.outcome)}
    >right</button>
  </div>

  <div class="knobs">
    <button
      class="toggle"
      role="switch"
      aria-checked={decisive}
      data-testid="rate-decisive"
      disabled={busy}
      onclick={() => onDecisive(!decisive)}
    >
      <span class="track" class:on={decisive}><span class="knob"></span></span>
      <span class="label data">{DECISIVE_LABEL}</span>
    </button>
    <span class="why decisive-why" data-testid="rate-decisive-why">{DECISIVE_COPY}</span>
    <button class="text" data-testid="rate-battle-skip" disabled={busy} onclick={onSkip}>
      skip
    </button>
  </div>

  <RateCorrections sides={card.corrections.sides} label={card.corrections.label} {busy} {onCorrect} />
</article>

<style>
  .battle {
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 16px 16px 0;
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: var(--r-lg);
    animation: fadeIn 0.15s ease;
  }
  .question {
    margin: 0;
    font-size: 17px;
    font-weight: 600;
    line-height: 1.25;
  }
  .pair {
    display: grid;
    grid-template-columns: 1fr auto 1fr;
    align-items: center;
    gap: 12px;
  }
  .side {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 0;
    background: none;
    border: none;
    cursor: pointer;
    text-align: left;
    color: inherit;
    -webkit-tap-highlight-color: transparent;
    touch-action: manipulation;
  }
  .side:hover:not(:disabled) :global(.poster),
  .side:focus-visible :global(.poster) {
    border-color: var(--ember);
    transform: translateY(-4px);
  }
  .side:active:not(:disabled) :global(.poster) {
    border-color: var(--ember-lift);
  }
  .side:disabled {
    opacity: 0.5;
    cursor: default;
  }
  .side.picked:disabled {
    opacity: 1;
  }
  .side.picked :global(.poster) {
    border-color: var(--ember);
  }
  .vs {
    letter-spacing: 0.12em;
  }
  .why {
    margin: 0;
  }
  .strip {
    display: flex;
    gap: 6px;
  }
  .cell {
    flex: 1;
    min-height: var(--touch);
    padding: 14px 8px;
    border-radius: var(--r-sm);
    border: 1px solid var(--line-2);
    background: var(--card-raised);
    color: var(--ink-2);
    font-family: var(--mono);
    font-size: 13px;
    cursor: pointer;
    transition: border-color 0.12s ease, color 0.12s ease;
  }
  .cell.tie {
    flex: 0 0 150px;
    opacity: 0.82;
  }
  .cell:hover:not(:disabled),
  .cell:focus-visible {
    border-color: var(--ember);
    color: var(--ink);
  }
  .cell:disabled {
    opacity: 0.45;
    cursor: default;
  }
  .cell.picked:disabled {
    opacity: 1;
    border-color: var(--ember);
    background: var(--ember-wash);
    color: var(--ink);
  }
  .knobs {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
  }
  .toggle {
    display: flex;
    align-items: center;
    gap: 9px;
    background: none;
    border: none;
    padding: 6px 0;
    cursor: pointer;
  }
  .track {
    width: 26px;
    height: 15px;
    border-radius: var(--r-pill);
    background: rgba(255, 255, 255, 0.14);
    position: relative;
    transition: background 0.12s ease;
    flex: none;
  }
  .track.on {
    background: var(--ember);
  }
  .knob {
    position: absolute;
    top: 2px;
    left: 2px;
    width: 11px;
    height: 11px;
    border-radius: var(--r-pill);
    background: var(--ink);
    transition: transform 0.12s ease;
  }
  .track.on .knob {
    transform: translateX(11px);
    background: var(--ember-ink);
  }
  .toggle .label {
    color: var(--ink-2);
  }
  .decisive-why {
    flex: 1;
    min-width: 200px;
  }
  .text {
    background: none;
    border: none;
    padding: 6px 0;
    color: var(--ink-4);
    font-family: var(--mono);
    font-size: 11.5px;
    text-decoration: underline dotted;
    text-underline-offset: 4px;
    cursor: pointer;
  }
  .text:hover:not(:disabled) {
    color: var(--ink-2);
  }

  @media (max-width: 720px) {
    .battle {
      padding: 12px 12px 0;
      gap: 8px;
    }
    .pair {
      gap: 8px;
    }
    .question {
      font-size: 15px;
    }
    .vs {
      display: none;
    }
    /* Phone: posters capped so the pair, the strip and the toggle row fit above the bottom bar. */
    .pair {
      grid-template-columns: 1fr 1fr;
      width: 100%;
      max-width: 200px;
      margin: 0 auto;
    }
    .strip {
      order: 1;
    }
    .knobs {
      order: 2;
      gap: 0 12px;
    }
    .battle > .why,
    .battle > .data {
      order: 3;
    }
    .battle > :global(.corrections) {
      order: 4;
    }
    .cell {
      border-radius: 2px;
    }
    .cell.tie {
      flex: 0 0 96px;
    }
    .decisive-why {
      order: 3;
      flex: 1 0 100%;
      min-width: 0;
    }
  }

  /* design.css's coarse floor sets height only, and `skip` would be 25px wide. */
  @media (pointer: coarse) {
    .text {
      min-width: var(--touch);
    }
  }
</style>
