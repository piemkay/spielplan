<script>
  // Ungated on purpose: the server strips `model` with show_model off, so the numbers never
  // reach the network tab or the cache (§6.7).
  let { model, compact = false } = $props();

  const n = (v, digits = 2) => (typeof v === 'number' ? v.toFixed(digits) : null);

  // The ledger half first: `s` is this person's own fitted number; the prior is the fallback.
  const all = $derived.by(() => {
    if (!model) return [];
    const out = [];
    if (n(model.s) !== null) {
      out.push(`s ${n(model.s)}${n(model.sigma) !== null ? ` ±${n(model.sigma)}` : ''}`);
    }
    if (n(model.cdf) !== null) out.push(`cdf ${n(model.cdf)}`);
    if (n(model.score) !== null) out.push(`score ${n(model.score)}`);
    if (n(model.b) !== null) out.push(`b(t) ${n(model.b)}`);
    if (n(model.beta) !== null) out.push(`β ${n(model.beta)}`);
    if (n(model.gate) !== null) out.push(`gate ${n(model.gate)}`);
    // §6.2's shared-sweet-spot extra, present only on that shelf.
    if (n(model.pair_score) !== null) out.push(`pair ${n(model.pair_score)}`);
    return out;
  });

  // A shelf card is 132px wide: compact shows two numbers, and the full line rides on `title`.
  const parts = $derived(compact ? all.slice(0, 2) : all);
</script>

{#if parts.length}
  <span class="note data" data-model-note title={all.join(' · ')}>{parts.join(' · ')}</span>
{/if}

<style>
  .note {
    display: block;
    line-height: 1.4;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--ink-4);
  }
</style>
