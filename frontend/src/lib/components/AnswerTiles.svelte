<script>
  // Rate's and the title card's four answers, worst to best, then Not seen a gap apart (decision
  // 527). `pending` names the answer in flight; `pressed`, when given, marks the standing one;
  // `failed` names one the server refused.
  import Icon from './Icon.svelte';

  let {
    answers,
    label,
    testid = undefined,
    pending = null,
    pressed = null,
    failed = null,
    disabled = false,
    compact = false,
    onAnswer
  } = $props();

  // A tap on the standing answer writes nothing, so its glyph flicks again to say it was heard.
  let replay = $state({ answer: null, n: 0 });

  function tap(a, standing) {
    replay = { answer: standing ? a.answer : null, n: replay.n + 1 };
    onAnswer(a);
  }
</script>

<div class="tiles" class:compact role="group" aria-label={label} data-testid={testid}>
  {#each answers as a (a.answer)}
    {@const on = pressed ? pressed(a) : undefined}
    {@const again = replay.answer === a.answer}
    <button
      class="tile press"
      class:picked={pending === a.answer || on}
      class:shake={failed === a.answer}
      data-answer={a.answer}
      data-flick={again || undefined}
      data-testid={a.testid}
      aria-pressed={on}
      aria-busy={pending === a.answer}
      {disabled}
      onclick={() => tap(a, on)}
    >{#key again && replay.n}<Icon name={a.answer} size={compact ? 22 : 24} />{/key}<span
        >{a.label}</span
      ></button>
  {/each}
</div>

<style>
  /* Five columns with an empty one before Not seen: four equal tiles, the last a gap apart. */
  .tiles {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr)) 0 minmax(0, 1fr);
    gap: 8px;
  }
  .tile[data-answer='not_seen'] {
    grid-column: 5;
  }
  /* Rate's single card: the three verdicts alone, Not seen under the poster (decision 529). */
  .tiles:not(:has([data-answer='not_seen'])) {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
  .tile {
    height: 64px;
    min-width: 0;
    padding: 0;
    border: none;
    border-radius: var(--r-md);
    background: var(--surface-1);
    color: var(--text);
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 4px;
    font-size: var(--fs-footnote);
    line-height: 18px;
    font-weight: 600;
  }
  .compact .tile {
    height: 60px;
  }
  @media (hover: hover) {
    .tile:hover:not(:disabled) {
      background: var(--surface-2);
    }
  }
  .tile:disabled {
    opacity: 0.45;
    cursor: default;
    transition-delay: var(--busy-delay);
  }
  /* The standing answer or the one in flight: a light fill, a selection and not the accent. */
  .tile.picked,
  .tile.picked:hover,
  .tile.picked:disabled {
    opacity: 1;
    background: var(--text);
    color: var(--bg);
  }
  .tile[aria-busy='true'] {
    animation: pop 180ms var(--ease);
  }
  .tiles:has([aria-busy='true']) .tile:not([aria-busy='true']) {
    opacity: 0.4;
    transform: scale(var(--press));
    transition-delay: var(--busy-delay);
  }
  .tile:is([aria-busy='true'], [data-flick])[data-answer='liked'] :global(svg) {
    animation: flick-up 180ms var(--ease-spring);
  }
  .tile:is([aria-busy='true'], [data-flick])[data-answer='disliked'] :global(svg) {
    animation: flick-down 180ms var(--ease-spring);
  }
  .tile:is([aria-busy='true'], [data-flick])[data-answer='fine'] :global(svg) {
    animation: swell 180ms var(--ease-spring);
  }
  @keyframes flick-up {
    from { transform: scale(0.8) rotate(-14deg); }
    55% { transform: translateY(-3px) scale(1.18); }
  }
  @keyframes flick-down {
    from { transform: scale(0.8) rotate(14deg); }
    55% { transform: translateY(3px) scale(1.18); }
  }
  @keyframes swell {
    from { transform: scale(0.85); }
    55% { transform: scale(1.15); }
  }

  @media (min-width: 981px) {
    .tiles:not(.compact) {
      gap: 12px;
    }
  }
</style>
