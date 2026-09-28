<script>
  // No model number before the answer (§6.1's anchoring rule). Only the five steps answer: a poster
  // opens "About this film", and Not seen under a film sends that side's correction (decision 528).
  import Icon from '$lib/components/Icon.svelte';
  import RatePoster from '$lib/components/RatePoster.svelte';
  import { PAIR_QUESTION, metaLine } from '$lib/rate.svelte.js';

  let { card, busy = false, pending = null, onDuel, onCorrect, onPeek, onWhy = null } = $props();

  const left = $derived(card?.left ?? {});
  const right = $derived(card?.right ?? {});
  const sides = $derived([
    ['left', left],
    ['right', right]
  ]);
  const unseen = $derived(card?.corrections?.sides ?? []);

  // Bigger at the ends, each side's two toward its poster.
  /** @type {[key: string, outcome: string, much: boolean, label: string, circle: string, icon: string[]][]} */
  const STEPS = [
    ['A-much', 'A', true, 'Much more', 'big', ['M11.5 6.5 6 12l5.5 5.5', 'M18 6.5 12.5 12l5.5 5.5']],
    ['A', 'A', false, 'More', 'mid', ['M15 6l-6 6 6 6']],
    ['TIE', 'TIE', false, 'Same', 'small', ['M6.5 9.5h11', 'M6.5 14.5h11']],
    ['B', 'B', false, 'More', 'mid', ['M9 6l6 6-6 6']],
    ['B-much', 'B', true, 'Much more', 'big', ['M6 6.5 11.5 12 6 17.5', 'M12.5 6.5 18 12l-5.5 5.5']]
  ];
  const ICON_PX = { big: 24, mid: 20, small: 16 };
  const aria = (outcome, much) =>
    outcome === 'TIE'
      ? 'About the same'
      : `${(outcome === 'A' ? left : right).name}: ${much ? 'much more' : 'more'}`;
  // The side an answer in flight favours: its poster rings, the other dims.
  const leaning = $derived(pending?.match(/^duel-([AB])/)?.[1] ?? null);
</script>

<article class="battle" data-testid="rate-battle-card" data-card-token={card?.token}>
  <div class="ask">
    <h2 class="question" data-testid="rate-battle-question">{PAIR_QUESTION}</h2>
    {#if card?.reason}
      <p class="sub">
        <span data-testid="rate-battle-reason">{card.reason}</span>{#if onWhy}{' · '}<button
            class="hit why-link"
            data-testid="rate-why"
            aria-haspopup="dialog"
            onclick={onWhy}>Why these?</button
          >{/if}
      </p>
    {/if}
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
        class="art"
        class:ringed={leaning === title.outcome}
        class:dimmed={leaning && leaning !== title.outcome}
        data-testid="rate-battle-{side}"
        data-outcome={title.outcome}
        data-title-id={title.id}
        aria-label="About {title.name ?? `the ${side} title`}"
        aria-haspopup="dialog"
        onclick={() => onPeek?.(side)}
      >
        <RatePoster {title} showName={false} />
        <span class="info" aria-hidden="true">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            stroke-width="1.75" stroke-linecap="round"><circle cx="12" cy="7.5" r="1"
              fill="currentColor" stroke="none" /><path d="M12 10.75v5.75" /></svg>
        </span>
      </button>
    {/each}
    {#each sides as [side, title] (side)}
      <div class="under">
        <span class="name">{title.name ?? '—'}</span>
        <span class="data">{metaLine(title)}</span>
        {#if unseen.includes(side)}
          <button
            class="hit unseen"
            data-testid="rate-correction-{side}"
            aria-label="Not seen: {title.name ?? side}"
            aria-busy={pending === `correction-${side}`}
            disabled={busy}
            onclick={() => onCorrect(side)}
          ><span class="face"><Icon name="eye-off" size={16} />Not seen</span></button>
        {/if}
      </div>
    {/each}
  </div>

  <div class="scale" role="group" aria-label={PAIR_QUESTION}>
    <span class="track" aria-hidden="true"></span>
    {#each STEPS as [key, outcome, much, label, circle, icon] (key)}
      <button
        class="step"
        class:picked={pending === `duel-${key}`}
        data-testid="rate-duel-{key}"
        aria-label={aria(outcome, much)}
        aria-busy={pending === `duel-${key}`}
        disabled={busy}
        onclick={() => onDuel(outcome, much)}
      >
        <span class="dot {circle}">
          <svg width={ICON_PX[circle]} height={ICON_PX[circle]} viewBox="0 0 24 24" fill="none"
            stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"
            aria-hidden="true">
            {#each icon as d (d)}<path {d} />{/each}
          </svg>
        </span>
        <span class="label">{label}</span>
      </button>
    {/each}
  </div>
</article>

<style>
  /* The posters take what height is left, 2:3 and never wider than their column (decision 528). */
  .battle {
    --gap: 12px;
    --col: min((100cqw - var(--gap)) / 2, 220px);
    container-type: inline-size;
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
    justify-content: space-evenly;
    gap: 12px;
    animation: fadeIn 0.15s var(--ease);
  }
  .ask,
  .scale {
    flex: none;
  }
  .ask {
    display: flex;
    flex-direction: column;
    align-items: center;
    text-align: center;
  }
  .question {
    margin: 0;
    font-family: var(--serif);
    font-weight: 400;
    font-size: 24px;
    line-height: 28px;
    text-wrap: balance;
  }
  p {
    margin: 0;
  }
  .sub {
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-2);
  }
  .why-link {
    padding: 0;
    border: none;
    background: none;
    color: var(--accent-text);
    font: inherit;
  }
  .pair {
    flex: 0 1 auto;
    min-height: 0;
    display: grid;
    grid-template-columns: repeat(2, var(--col));
    grid-template-rows: minmax(0, calc(var(--col) * 1.5)) auto;
    column-gap: var(--gap);
    justify-content: center;
  }
  .art {
    position: relative;
    justify-self: center;
    height: 100%;
    aspect-ratio: 2 / 3;
    padding: 0;
    border: none;
    border-radius: var(--r-poster);
    background: none;
    -webkit-tap-highlight-color: transparent;
    transition: transform 0.18s var(--ease), opacity 0.18s var(--ease), box-shadow 0.18s var(--ease);
  }
  .art.ringed {
    transform: scale(1.02);
    box-shadow: 0 0 0 2px var(--accent);
  }
  .art.dimmed {
    opacity: 0.6;
  }
  .info {
    position: absolute;
    top: 8px;
    right: 8px;
    display: grid;
    place-items: center;
    width: 24px;
    height: 24px;
    border-radius: var(--r-pill);
    background: rgba(12, 11, 10, 0.72);
    color: var(--text);
  }
  .under {
    min-width: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    padding-top: 6px;
    text-align: center;
  }
  .name,
  .under .data {
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .name {
    font-size: var(--fs-subhead);
    line-height: 20px;
    font-weight: 600;
  }
  .under .data {
    line-height: 18px;
  }
  .unseen {
    height: 44px;
    min-height: 44px;
    margin: 2px 0 -6px;
    padding: 0;
    border: none;
    background: none;
    color: var(--text-2);
  }
  .face {
    height: 32px;
    padding: 0 12px;
    display: flex;
    align-items: center;
    gap: 6px;
    border-radius: var(--r-pill);
    background: var(--surface-2);
    font-size: var(--fs-footnote);
    line-height: 18px;
    font-weight: 600;
    white-space: nowrap;
  }
  .unseen:disabled {
    opacity: 0.45;
  }
  /* Five answers on one track, the outer two under their poster's column. */
  .scale {
    position: relative;
    align-self: center;
    width: calc(var(--col) * 2 + var(--gap));
    display: grid;
    grid-template-columns: repeat(5, minmax(0, 1fr));
  }
  .track {
    position: absolute;
    left: 10%;
    right: 10%;
    top: 25px;
    height: 2px;
    border-radius: 1px;
    background: var(--progress-track);
  }
  .step {
    position: relative;
    min-width: 0;
    height: 74px;
    padding: 0;
    border: none;
    background: none;
    color: var(--text);
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 6px;
    -webkit-tap-highlight-color: transparent;
  }
  .dot {
    height: 52px;
    display: grid;
    place-items: center;
  }
  .dot::before {
    content: '';
    position: absolute;
    top: calc(26px - var(--d) / 2);
    left: calc(50% - var(--d) / 2);
    width: var(--d);
    height: var(--d);
    box-sizing: border-box;
    border-radius: var(--r-pill);
    border: 1.5px solid rgba(245, 240, 232, 0.28);
    background: var(--surface-2);
    transition: background 0.12s var(--ease), transform 0.12s var(--ease);
  }
  .dot svg {
    position: relative;
  }
  .big {
    --d: 52px;
  }
  .big::before {
    background: var(--surface-3);
  }
  .mid {
    --d: 40px;
  }
  .small {
    --d: 32px;
  }
  .label {
    font-size: var(--fs-caption);
    line-height: 16px;
    color: var(--text-3);
    white-space: nowrap;
  }
  @media (hover: hover) {
    .step:hover:not(:disabled) .dot::before {
      background: var(--thumb);
    }
  }
  .step:active:not(:disabled) .dot::before {
    transform: scale(0.94);
  }
  .step.picked .dot::before {
    border-color: var(--accent);
    background: var(--accent);
  }
  .step.picked {
    color: var(--on-accent);
  }
  .step:disabled:not(.picked) {
    opacity: 0.45;
  }

  @media (min-width: 721px) {
    .battle {
      --gap: 32px;
    }
    .question {
      font-size: var(--fs-title);
      line-height: 34px;
    }
  }
</style>
