<script>
  /**
   * One inline numeric annotation, in the data voice. Spec v2.1 §6.7 (decision 117), §6.8.
   *
   * Decision 117: the "show the model" toggle "governs the event rail **and** every inline
   * numeric annotation". The gate is not here — the server strips the whole `model` block from
   * the payload when the toggle is off, so this component simply does not get rendered. That
   * is deliberate: a gate in CSS or in an `{#if}` leaves the numbers in the network tab and in
   * the service-worker cache, and §6.7's promise is that they are not there at all.
   *
   * §6.8: "model numbers appear in the data voice next to their name … never bare." Every part
   * below carries its name, which is why this is a component rather than a template string.
   */
  let { model, compact = false } = $props();

  const n = (v, digits = 2) => (typeof v === 'number' ? v.toFixed(digits) : null);

  /**
   * The ledger half first when it exists — `s` is this person's own fitted number and is the
   * one worth reading; the prior half (`b`, `gate`, support) is what the shelf fell back to
   * when they have not rated the title.
   */
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
    if (typeof model.item_n === 'number') out.push(`n=${model.item_n}`);
    if (model.e_source) out.push(model.e_source);
    // §6.2's shared-sweet-spot extras, present only on that shelf.
    if (n(model.pair_score) !== null) out.push(`pair ${n(model.pair_score)}`);
    return out;
  });

  // A shelf card is 132 px wide, so the compact form shows the two most specific numbers it
  // has — the ledger pair when the title is rated, the serving score and the prior when it is
  // not. The full line still travels, on the `title` attribute: §6.8's rule is that a number
  // never appears bare, not that every number appears.
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
