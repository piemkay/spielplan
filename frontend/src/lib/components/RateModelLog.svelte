<script>
  // The caller renders this only with show_model on; lines are the last response's, never stored.
  let { log = [], ledger = null } = $props();

  // Built in JS: Svelte collapses the whitespace around an `{#each}`, gluing separators to numbers.
  const ledgerLine = $derived.by(() => {
    if (!ledger) return '';
    if (!ledger.applied) return `ledger update refused · ${ledger.reason}`;
    return [
      `ledger ${ledger.kind} · ${ledger.refit ? 'refit' : 'fold-in'} ${ledger.ms} ms`,
      ...(ledger.rows ?? []).map(
        (r) =>
          `title ${r.title_id} tier ${r.tier}` +
          (r.cdf === null || r.cdf === undefined ? '' : ` cdf ${r.cdf.toFixed(2)}`)
      )
    ].join(' · ');
  });
</script>

{#if log.length || ledger}
  <section class="card log" data-testid="rate-model-log">
    <h3>Model log</h3>
    {#each log as line, i (i)}
      <div class="line" data-testid="rate-model-log-line">{line}</div>
    {/each}
    {#if ledgerLine}
      <div class="line" data-testid="rate-ledger-line">{ledgerLine}</div>
    {/if}
  </section>
{/if}

<style>
  .log {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  h3 {
    margin: 0 0 4px;
    font-size: var(--fs-subhead);
    line-height: 20px;
    font-weight: 600;
  }
  /* The data voice: tabular figures in the interface face. */
  .line {
    font-size: var(--fs-footnote);
    line-height: 18px;
    font-variant-numeric: tabular-nums;
    color: var(--text-2);
    word-break: break-word;
  }
</style>
