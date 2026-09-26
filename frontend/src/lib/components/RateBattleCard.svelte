<script>
  /**
   * §6.1's battle card: "two posters are the buttons; `Tie` (feeds the Davidson tie term); a
   * **decisive switch** … for the pair on the table and is off again for the next pair …
   * Corrections zone at the bottom (nothing tappable inside the poster cards), one row"
   * (decision 520).
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
   * The decisive switch lives on the server and belongs to the pair on the table: it weighs
   * the answer to this pair and is off again for the next (decision 520 — on the second
   * household test a switch that stayed on weighed a pick nobody had marked as clear). It
   * carries its own one-line why (proposal 47) rather than leaving the justification two cards
   * down the rail. Long-press on a poster is §6.1's single gesture accelerator (proposal 51):
   * one decisive answer, without moving the switch.
   *
   * And the card asks its question. It showed two posters over `left | tie | right` and asked
   * nothing, which both members of the second household test stopped at (A1).
   */
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

  /**
   * The press names the card it started on.
   *
   * A long press is a write that happens 500 ms after the finger lands, and the only thing that
   * can change in that window is which pair is on the table: the `?head=` effect, the model-gate
   * effect and Undo all call `load()`, and `rate.svelte.js` read `rate.card.token` at fire time.
   * The timer closed over the outcome alone, so the gesture landed on whatever card had arrived —
   * reproduced as a decisive duel posted against the pair that replaced the pressed one. It is
   * the strongest observation the app has (§5.2 weighs decisive ~1.6 against ~1.0), §4.2 keeps it
   * forever, and nothing in the Ledger tells it apart from one the person made. So the token is
   * captured here, the timer drops itself when the card has moved on, and `duel()` refuses the
   * write as well, because the card can still change between this check and the POST.
   * [§6.1, §5.2, §4.2; M4.10 finding 28]
   */
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

  // A timer still armed when this card goes away — a navigation, a mode switch, a sweep card
  // taking the slot — used to fire into a component nobody is looking at and write a duel. The
  // token check above would now refuse most of those; unarming it refuses all of them, and it is
  // the same teardown `rate/+page.svelte` and `rank/+page.svelte` already do with `reset`.
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
      class:picked={pending === `duel-${left.outcome ?? 'A'}`}
      data-testid="rate-battle-left"
      aria-label="Pick {left.name ?? 'the left title'}"
      aria-busy={pending === `duel-${left.outcome ?? 'A'}`}
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
      class:picked={pending === `duel-${right.outcome ?? 'B'}`}
      data-testid="rate-battle-right"
      aria-label="Pick {right.name ?? 'the right title'}"
      aria-busy={pending === `duel-${right.outcome ?? 'B'}`}
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

  <!-- A battle only ever stands in for a sweep when there is nothing new to rate (§6.1's drained
       state), so this line can name its cause, in the member's words (decision 486). -->
  {#if card?.substituted_for}
    <p class="data" data-testid="rate-substituted">
      Nothing new to rate right now - comparing titles you've already rated.
    </p>
  {/if}

  <!-- Proposal 48: the mirrored strip, one-handed, and the home of Tie. -->
  <div class="strip" role="group" aria-label={PAIR_QUESTION}>
    <button
      class="cell"
      class:picked={pending === `duel-${left.outcome ?? 'A'}`}
      data-testid="rate-strip-left"
      disabled={busy}
      onclick={() => onDuel(left.outcome ?? 'A')}
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
      class:picked={pending === `duel-${right.outcome ?? 'B'}`}
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
      <span class="label data">{DECISIVE_LABEL}</span>
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
  /* The answer in flight stays lit while the rest wait (A4 of the 2026-09-26 household test). */
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
    /* On an iPhone 13 the card used to stand 589 px tall: two full-width 2:3 posters, then the
       why-line, and Tie, §6.1's decisive switch and Skip all below the bottom bar.
       Measured at 390 x 664 the posters are now capped so the pair, the strip and the toggle
       row fit above it, and the order under the posters is the order a thumb needs them in:
       the answer strip, the toggle and Skip, then the why-line and the corrections row. The
       posters are still the buttons (§6.1), and the name sits on each (§6.8's card grammar).
       [§6 preamble; C5.6 of the 2026-09-25 household test] */
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

  /* The same rule and the same four characters as `RateSweepCard`'s, for the same reason: a
     `button` clears design.css's coarse block on the axis that block sets and goes unmeasured on
     the one it does not, and `skip` with `padding: 6px 0` is 25 px wide. `.knobs` wraps, so on a
     phone this is often the only control on its row and there is nothing beside it to widen it.
     [§6 preamble; proposal 38; review cycle 3: M415-C3-CSS-01] */
  @media (pointer: coarse) {
    .text {
      min-width: var(--touch);
    }
  }
</style>
