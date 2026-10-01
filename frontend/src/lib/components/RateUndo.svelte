<script>
  // Always on screen, dimmed with nothing to take back (decision 550); `data-undo-kind` keeps the
  // journal's raw kind for tests.
  let { undo, pending = false, onUndo } = $props();

  const available = $derived(!!undo?.available);
  const label = $derived(
    !available
      ? 'Undo'
      : undo.kind === 'placement'
        ? `Undo placing ${undo.name}`
        : undo.kind === 'not_seen'
          ? 'Undo not seen'
          : 'Undo'
  );
</script>

<button
  class="hit undo"
  data-testid="rate-undo"
  aria-label={label}
  data-undo-kind={undo?.kind ?? ''}
  aria-busy={pending}
  disabled={!available}
  onclick={onUndo}
>
  <span class="disc">
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
      stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path d="M9 14 4.5 9.5 9 5" />
      <path d="M4.5 9.5H15a5 5 0 0 1 0 10h-3" />
    </svg>
  </span>
</button>

<style>
  .undo {
    width: 44px;
    height: 44px;
    min-height: 44px;
    padding: 0;
    border: none;
    background: none;
    display: flex;
    align-items: center;
    justify-content: flex-start;
  }
  .disc {
    width: 36px;
    height: 36px;
    display: grid;
    place-items: center;
    border-radius: var(--r-pill);
    background: var(--surface-2);
    color: var(--text);
    transition: color var(--dur-quick) var(--ease);
  }
  .undo:disabled {
    cursor: default;
  }
  .undo:disabled .disc {
    color: rgba(245, 240, 232, 0.3);
  }
  .undo[aria-busy='true'] svg {
    animation: unwind 240ms var(--ease-spring);
  }
  @keyframes unwind {
    from {
      transform: rotate(-40deg);
    }
  }
</style>
