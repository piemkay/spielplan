<script>
  /**
   * §6 preamble: "undo everywhere". §6.1: "+ persistent Undo". Decision 35 fixes its depth and,
   * just as importantly, its failure mode:
   *
   *   "Undo reaches back to the start of the current block of 15 and no further — the journal
   *    is bounded by the block, the depth matches the counter the user is already reading
   *    ('7 / 15 this block'), and the chip disables visibly, not silently, at the boundary."
   *
   * So this chip is never hidden and never a button that quietly does nothing. When the server
   * says `available: false` it also says why, and the reason is on screen next to the disabled
   * control rather than discovered by tapping it. The kind of the observation waiting to be
   * popped rides along too — "undo verdict" is a promise the person can check before making it.
   */
  import { undoMessage } from '$lib/rate.svelte.js';

  let { undo, busy = false, onUndo } = $props();

  const available = $derived(!!undo?.available);
  const reason = $derived(undoMessage(undo));
</script>

<div class="wrap">
  <button
    class="chip"
    data-testid="rate-undo"
    aria-label={available && undo?.kind ? `Undo the last ${undo.kind}` : 'Undo'}
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
    <span>undo{available && undo?.kind ? ` ${undo.kind}` : ''}</span>
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
</style>
