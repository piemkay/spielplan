<script>
  // Rate's and the title card's four answers, worst to best, then Not seen a gap apart (decision
  // 527). `pending` names the answer in flight; `pressed`, when given, marks the standing one.
  let {
    answers,
    label,
    testid = undefined,
    pending = null,
    pressed = null,
    disabled = false,
    compact = false,
    onAnswer
  } = $props();

  const THUMB = [
    'M7 10.5V20H4.5a1 1 0 0 1-1-1v-7.5a1 1 0 0 1 1-1z',
    'M7 10.5 10.8 3.6a1.9 1.9 0 0 1 3.5 1.3L13.4 9h5.2a2 2 0 0 1 2 2.4l-1.4 7A2 2 0 0 1 17.2 20H7'
  ];
</script>

{#snippet icon(answer)}
  <svg width={compact ? 22 : 24} height={compact ? 22 : 24} viewBox="0 0 24 24" fill="none"
    stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"
    aria-hidden="true">
    {#if answer === 'disliked'}
      <g transform="rotate(180 12 12)">{#each THUMB as d (d)}<path {d} />{/each}</g>
    {:else if answer === 'fine'}
      <circle cx="12" cy="12" r="8.5" /><path d="M8.5 14.5h7" /><path d="M9.2 9.8h.01M14.8 9.8h.01" />
    {:else if answer === 'liked'}
      {#each THUMB as d (d)}<path {d} />{/each}
    {:else}
      <path d="M3.5 3.5l17 17" />
      <path d="M10.6 5.1A9.6 9.6 0 0 1 12 5c5 0 8.5 4.5 9.5 7a13 13 0 0 1-2.7 3.9" />
      <path d="M6.6 6.6C4.6 7.9 3.2 9.9 2.5 12c1 2.5 4.5 7 9.5 7 1.7 0 3.2-.5 4.6-1.2" />
      <path d="M9.9 9.9a3 3 0 0 0 4.2 4.2" />
    {/if}
  </svg>
{/snippet}

<div class="tiles" class:compact role="group" aria-label={label} data-testid={testid}>
  {#each answers as a (a.answer)}
    {@const on = pressed ? pressed(a) : undefined}
    <button
      class="tile"
      class:picked={pending === a.answer || on}
      data-answer={a.answer}
      data-testid={a.testid}
      aria-pressed={on}
      aria-busy={pending === a.answer}
      {disabled}
      onclick={() => onAnswer(a)}
    >{@render icon(a.answer)}<span>{a.label}</span></button>
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
    transition: background 0.12s var(--ease), transform 0.12s var(--ease);
  }
  .compact .tile {
    height: 60px;
  }
  @media (hover: hover) {
    .tile:hover:not(:disabled) {
      background: var(--surface-2);
    }
  }
  .tile:active:not(:disabled) {
    transform: scale(0.97);
    background: var(--surface-2);
  }
  .tile:disabled {
    opacity: 0.45;
    cursor: default;
  }
  /* The standing answer or the one in flight: a light fill, a selection and not the accent. */
  .tile.picked,
  .tile.picked:hover,
  .tile.picked:disabled {
    opacity: 1;
    background: var(--text);
    color: var(--bg);
  }

  @media (min-width: 981px) {
    .tiles:not(.compact) {
      gap: 12px;
    }
    .tiles:not(.compact) .tile {
      height: 100px;
      gap: 8px;
      font-size: var(--fs-subhead);
      line-height: 20px;
    }
  }
</style>
