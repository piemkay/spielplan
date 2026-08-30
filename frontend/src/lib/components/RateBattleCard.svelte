<script>
  /**
   * §6.1's battle card: "two posters are the buttons; `Tie` (feeds the Davidson tie term); a
   * persistent **decisive toggle** … Corrections zone at the bottom (nothing tappable inside
   * the poster cards), one row".
   *
   * Four things this card does not have, each on purpose:
   *
   *   * **No tier, score, σ or rank.** Proposal 34 extends §6.1's anchoring rule to battles —
   *     "as drawn in the prototype, every duel is anchored on exactly the quantity it exists to
   *     correct". Year and runtime only.
   *   * **Nothing tappable inside the poster.** The poster *is* the target; a nested control
   *     would make the biggest tap area on the card ambiguous.
   *   * **No hidden Tie.** Proposal 48 keeps the mirrored `left | tie | right` strip below the
   *     posters: on a phone it is the only thumb-reachable target, and it is where Tie lives.
   *   * **No skipped ties.** `Tie` is an outcome that writes a duel row (22% of random pairs
   *     are genuine ties), never a dropped question.
   *
   * The decisive toggle is persistent and lives on the server, so it survives this card, this
   * session and this device — and it carries its own one-line why (proposal 47) rather than
   * leaving the justification two cards down the rail. Long-press on a poster is §6.1's single
   * gesture accelerator (proposal 51): one decisive answer, without moving the toggle.
   */
  import RateCorrections from '$lib/components/RateCorrections.svelte';
  import RatePoster from '$lib/components/RatePoster.svelte';
  import { DECISIVE_COPY, metaLine } from '$lib/rate.svelte.js';

  let {
    card,
    decisive = false,
    busy = false,
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

  function press(outcome) {
    fired = false;
    clearTimeout(timer);
    timer = setTimeout(() => {
      fired = true;
      onDuel(outcome, { decisive: true });
    }, LONG_PRESS_MS);
  }

  function release() {
    clearTimeout(timer);
    timer = null;
  }

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
  <div class="pair">
    <button
      class="side"
      data-testid="rate-battle-left"
      aria-label="Pick {left.name ?? 'the left title'}"
      data-outcome={left.outcome ?? 'A'}
      data-title-id={left.id}
      disabled={busy}
      onpointerdown={() => press(left.outcome ?? 'A')}
      onpointerup={release}
      onpointerleave={release}
      onpointercancel={release}
      onclick={() => tap(left.outcome ?? 'A')}
    >
      <RatePoster title={left} />
      <span class="data">{metaLine(left)}</span>
    </button>

    <span class="vs data" aria-hidden="true">vs</span>

    <button
      class="side"
      data-testid="rate-battle-right"
      aria-label="Pick {right.name ?? 'the right title'}"
      data-outcome={right.outcome ?? 'B'}
      data-title-id={right.id}
      disabled={busy}
      onpointerdown={() => press(right.outcome ?? 'B')}
      onpointerup={release}
      onpointerleave={release}
      onpointercancel={release}
      onclick={() => tap(right.outcome ?? 'B')}
    >
      <RatePoster title={right} />
      <span class="data">{metaLine(right)}</span>
    </button>
  </div>

  <!-- §6.8's one-line why, on the question as well as on the shelf. -->
  <p class="why" data-testid="rate-battle-reason">{card?.reason ?? ''}</p>

  {#if card?.substituted_for}
    <p class="data" data-testid="rate-substituted">
      the sweep queue is drained for now — sharpening what you have already said
    </p>
  {/if}

  <!-- Proposal 48: the mirrored strip, one-handed, and the home of Tie. -->
  <div class="strip" role="group" aria-label="Which one">
    <button
      class="cell"
      data-testid="rate-strip-left"
      disabled={busy}
      onclick={() => onDuel(left.outcome ?? 'A')}
    >left</button>
    <button
      class="cell tie"
      data-testid="rate-strip-tie"
      disabled={busy}
      onclick={() => onDuel('TIE')}
    >tie</button>
    <button
      class="cell"
      data-testid="rate-strip-right"
      disabled={busy}
      onclick={() => onDuel(right.outcome ?? 'B')}
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
      <span class="label data">decisive</span>
    </button>
    <span class="why decisive-why" data-testid="rate-decisive-why">{DECISIVE_COPY}</span>
    <button class="text" data-testid="rate-battle-skip" disabled={busy} onclick={onSkip}>
      skip
    </button>
  </div>

  <RateCorrections
    sides={card?.corrections?.sides ?? ['left', 'both', 'right']}
    label={card?.corrections?.label ?? 'not seen'}
    {busy}
    {onCorrect}
  />
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
    }
    .pair {
      gap: 8px;
    }
    .vs {
      display: none;
    }
    .pair {
      grid-template-columns: 1fr 1fr;
    }
    .cell {
      border-radius: 2px;
    }
    .cell.tie {
      flex: 0 0 96px;
    }
    .decisive-why {
      flex: 1 0 100%;
      min-width: 0;
    }
  }
</style>
