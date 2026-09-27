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
    onDecisive,
    onWhy
  } = $props();

  const left = $derived(card?.left ?? {});
  const right = $derived(card?.right ?? {});
  const sides = $derived([
    ['left', left],
    ['right', right]
  ]);

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
  <div class="ask">
    <h2 class="title-1" data-testid="rate-battle-question">{PAIR_QUESTION}</h2>
    <p class="why">
      <span data-testid="rate-battle-reason">{card?.reason ?? ''}</span>
      <button class="hit why-link" data-testid="rate-why" onclick={onWhy}>Why these?</button>
    </p>
    <!-- A pair stands in for a single title only when nothing new is left to rate (§6.1). -->
    {#if card?.substituted_for}
      <p class="footnote" data-testid="rate-substituted">
        Nothing new to rate right now — comparing titles you've already rated.
      </p>
    {/if}
  </div>

  <div class="pair">
    {#each sides as [side, title] (side)}
      <button
        class="side"
        class:picked={pending === `duel-${title.outcome}`}
        data-testid="rate-battle-{side}"
        aria-label="Pick {title.name ?? `the ${side} title`}"
        aria-busy={pending === `duel-${title.outcome}`}
        data-outcome={title.outcome}
        data-title-id={title.id}
        disabled={busy}
        onpointerdown={() => press(title.outcome)}
        onpointerup={release}
        onpointerleave={release}
        onpointercancel={release}
        onclick={() => tap(title.outcome)}
      >
        <span class="art"><RatePoster {title} showName={false} /></span>
        <span class="name">{title.name ?? '—'}</span>
        <span class="data">{metaLine(title)}</span>
      </button>
    {/each}
  </div>

  <div class="more">
    <button
      class="btn-secondary tie"
      class:picked={pending === 'duel-TIE'}
      data-testid="rate-strip-tie"
      aria-busy={pending === 'duel-TIE'}
      disabled={busy}
      onclick={() => onDuel('TIE')}
    >About the same</button>

    <button
      class="favourite"
      role="switch"
      aria-checked={decisive}
      aria-labelledby="rate-decisive-label"
      aria-describedby="rate-decisive-why"
      data-testid="rate-decisive"
      disabled={busy}
      onclick={() => onDecisive(!decisive)}
    >
      <span class="words">
        <span class="label" id="rate-decisive-label">{DECISIVE_LABEL}</span>
        <span class="footnote" id="rate-decisive-why" data-testid="rate-decisive-why">{DECISIVE_COPY}</span>
      </span>
      <span class="track" class:on={decisive}><span class="knob"></span></span>
    </button>
  </div>

  <RateCorrections
    sides={card.corrections.sides}
    names={{ left: left.name, right: right.name }}
    {busy}
    {onCorrect}
  />
</article>

<style>
  .battle {
    display: flex;
    flex-direction: column;
    gap: 24px;
    animation: fadeIn 0.15s var(--ease);
  }
  .ask {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 4px;
    text-align: center;
  }
  h2 {
    text-wrap: balance;
  }
  p {
    margin: 0;
  }
  .why-link {
    padding: 0;
    border: none;
    background: none;
    color: var(--accent-text);
    font: inherit;
  }
  .pair {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 160px));
    justify-content: center;
    gap: 16px;
  }
  .side {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 2px;
    min-width: 0;
    padding: 0;
    border: none;
    background: none;
    color: var(--text);
    text-align: center;
    -webkit-tap-highlight-color: transparent;
    touch-action: manipulation;
  }
  /* The width follows the screen's height too, so the pair and Tie fit above the tab bar. */
  .art {
    display: block;
    width: min(100%, 20dvh);
    aspect-ratio: 2 / 3;
    margin: 0 auto 6px;
    border-radius: var(--r-poster);
    transition: transform 0.18s var(--ease), box-shadow 0.18s var(--ease);
  }
  .name {
    font-size: var(--fs-body);
    line-height: 22px;
    font-weight: 600;
    text-wrap: balance;
  }
  .side:hover:not(:disabled) .art,
  .side:focus-visible .art {
    transform: translateY(-4px);
  }
  .side:active:not(:disabled) .art {
    transform: scale(0.97);
  }
  .side:disabled {
    opacity: 0.5;
    cursor: default;
  }
  .side.picked:disabled {
    opacity: 1;
  }
  .side.picked .art {
    box-shadow: 0 0 0 2px var(--text);
  }
  .more {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .tie {
    width: 100%;
  }
  .tie.picked:disabled {
    opacity: 1;
    background: var(--text);
    color: var(--bg);
  }
  .favourite {
    min-height: 52px;
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 0;
    border: none;
    background: none;
    color: var(--text);
    text-align: left;
  }
  .favourite:disabled {
    cursor: default;
  }
  .words {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
  }
  .label {
    font-size: var(--fs-body);
    line-height: 22px;
  }
  .track {
    width: 51px;
    height: 31px;
    flex: none;
    display: flex;
    padding: 2px;
    border-radius: var(--r-pill);
    background: var(--progress-track);
    transition: background 0.18s var(--ease);
  }
  .track.on {
    justify-content: flex-end;
    background: var(--accent);
  }
  .knob {
    width: 27px;
    height: 27px;
    border-radius: var(--r-pill);
    background: var(--text);
    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.3);
  }

  /* A short phone screen: tighter, so the pair and its answers stay in view. */
  @media (max-height: 700px) {
    .battle {
      gap: 16px;
    }
  }

  @media (min-width: 981px) {
    .pair {
      grid-template-columns: repeat(2, minmax(0, 220px));
      gap: 32px;
    }
    .art {
      width: min(100%, 27dvh);
    }
    .more,
    .battle > :global(.corrections) {
      width: 100%;
      max-width: 472px;
      margin: 0 auto;
    }
  }
</style>
