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
  <section class="log" data-testid="rate-model-log">
    <span class="eyebrow">MODEL LOG</span>
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
    gap: 5px;
    padding: 11px 13px;
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: var(--r-md);
  }
  .eyebrow {
    font-family: var(--mono);
    font-size: 9.5px;
    letter-spacing: 0.12em;
    color: var(--ink-4);
  }
  .line {
    font-family: var(--mono);
    font-size: 10.5px;
    line-height: 1.55;
    color: var(--ink-3);
    word-break: break-word;
  }
</style>
