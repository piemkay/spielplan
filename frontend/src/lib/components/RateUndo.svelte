<script>
  // Never hidden: at the block boundary it disables with the server's reason beside it (decision 35).
  // Two root nodes on purpose: the page's header grid places the reason on a row of its own.
  import { undoKindLabel, undoMessage } from '$lib/rate.svelte.js';

  let { undo, busy = false, onUndo } = $props();

  const available = $derived(!!undo?.available);
  const reason = $derived(undoMessage(undo));
  // Words, never the journal's column value; `data-undo-kind` keeps the raw kind for tests.
  const kindWords = $derived(available ? undoKindLabel(undo?.kind) : '');
</script>

<button
  class="btn-plain undo"
  data-testid="rate-undo"
  aria-label={kindWords ? `Undo the last ${kindWords}` : 'Undo'}
  data-undo-kind={undo?.kind ?? ''}
  data-undo-reason={undo?.reason ?? ''}
  disabled={!available || busy}
  aria-describedby={available ? undefined : 'rate-undo-reason'}
  onclick={onUndo}
>
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <path d="M9 14 4.5 9.5 9 5" />
    <path d="M4.5 9.5H15a5 5 0 0 1 0 10h-3" />
  </svg>
  <span>Undo</span>
</button>
{#if !available}
  <p class="footnote undo-reason" id="rate-undo-reason" data-testid="rate-undo-reason">{reason}</p>
{/if}

<style>
  .undo {
    justify-self: start;
    padding-left: 0;
  }
  .undo-reason {
    margin: 0;
  }
</style>
