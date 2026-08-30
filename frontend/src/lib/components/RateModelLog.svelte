<script>
  /**
   * §6.7's transparency rail, for the writes this surface makes.
   *
   *   "A per-user toggle (default off) reveals an ephemeral log (last ~15 events, never
   *    persisted) narrating every model write in one human-readable line."
   *
   * Two things follow from that sentence and are enforced here rather than assumed.
   *
   *   * **Gated.** The caller renders this only when the person's `show_model` preference is
   *     on. Everything in it — the incremental refit's milliseconds, the title's new `cdf` and
   *     tier — is model-derived, and §6.1's anchoring rule is why it can be shown at all: all
   *     of it describes a write that has already happened, on a card that is already answered.
   *   * **Ephemeral.** The lines come from the last response and are replaced by the next one.
   *     Nothing accumulates in local storage; the log is a view of the current write, which is
   *     what makes it "never persisted" on the client too.
   */
  let { log = [], ledger = null } = $props();

  /**
   * Built in JS rather than in markup: Svelte collapses the whitespace around an `{#each}`,
   * which printed "tier 4· title 1012" with the separator glued to the number.
   */
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
