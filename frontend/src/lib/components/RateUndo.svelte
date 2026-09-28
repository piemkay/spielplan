<script>
  // Never hidden: with nothing it can take back it shows disabled (decisions 35 and 528).
  import { undoKindLabel } from '$lib/rate.svelte.js';

  let { undo, busy = false, pending = false, onUndo } = $props();

  const available = $derived(!!undo?.available);
  // Words, never the journal's column value; `data-undo-kind` keeps the raw kind for tests.
  const kindWords = $derived(available ? undoKindLabel(undo?.kind) : '');
</script>

<button
  class="btn-plain hit undo"
  data-testid="rate-undo"
  aria-label={kindWords ? `Undo the last ${kindWords}` : 'Undo'}
  data-undo-kind={undo?.kind ?? ''}
  data-undo-reason={undo?.reason ?? ''}
  aria-busy={pending}
  disabled={!available || busy}
  onclick={onUndo}
>
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <path d="M9 14 4.5 9.5 9 5" />
    <path d="M4.5 9.5H15a5 5 0 0 1 0 10h-3" />
  </svg>
  <span>Undo</span>
</button>

<style>
  .undo {
    justify-self: start;
    padding-left: 0;
    transition: opacity var(--dur-quick) var(--ease);
  }
  .undo:disabled {
    opacity: 0.35;
  }
  .undo[aria-busy='true'] {
    opacity: 1;
  }
  .undo[aria-busy='true'] svg {
    animation: unwind 240ms var(--ease-spring);
  }
  @keyframes unwind {
    from { transform: rotate(-40deg); }
  }
</style>
