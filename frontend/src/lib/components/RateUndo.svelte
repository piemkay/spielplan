<script>
  // Never hidden: at the block boundary it disables with the server's reason beside it (decision 35).
  import { undoKindLabel, undoMessage } from '$lib/rate.svelte.js';

  let { undo, busy = false, onUndo } = $props();

  const available = $derived(!!undo?.available);
  const reason = $derived(undoMessage(undo));
  // Words, never the journal's column value; `data-undo-kind` keeps the raw kind for tests.
  const kindWords = $derived(available ? undoKindLabel(undo?.kind) : '');
</script>

<div class="wrap">
  <button
    class="chip"
    data-testid="rate-undo"
    aria-label={kindWords ? `Undo the last ${kindWords}` : 'Undo'}
    data-undo-kind={undo?.kind ?? ''}
    data-undo-reason={undo?.reason ?? ''}
    disabled={!available || busy}
    aria-describedby={available ? undefined : 'rate-undo-reason'}
    onclick={onUndo}
  >
    <svg width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor"
      stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path d="M7 5 3.5 8.5 7 12" />
      <path d="M3.5 8.5H12a4.5 4.5 0 0 1 0 9h-3" />
    </svg>
    <span>undo{kindWords ? ` ${kindWords}` : ''}</span>
  </button>
  {#if !available}
    <span class="why" id="rate-undo-reason" data-testid="rate-undo-reason">{reason}</span>
  {/if}
</div>

<style>
  .wrap {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }
  .chip {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 8px 16px;
    border-radius: var(--r-pill);
    border: 1px solid var(--line-2);
    background: transparent;
    color: var(--ink-2);
    font-family: var(--mono);
    font-size: 12px;
    cursor: pointer;
    transition: border-color 0.12s ease, color 0.12s ease;
  }
  .chip:hover:not(:disabled),
  .chip:focus-visible {
    border-color: var(--ember);
    color: var(--ink);
  }
  .chip:disabled {
    opacity: 0.4;
    cursor: not-allowed;
    color: var(--ink-4);
  }
  .why {
    max-width: 40ch;
  }
  @media (pointer: coarse) {
    .chip {
      min-height: var(--touch);
    }
  }
  /* Phone: the chip joins the parent row and the reason wraps onto its own line. */
  @media (max-width: 720px) {
    .wrap {
      display: contents;
    }
    .why {
      order: 10;
      flex: 1 1 100%;
      max-width: none;
    }
  }
</style>
