<script>
  // The review and the two editors stay apart: each has its own semantics (decisions 341, 445).
  import DnaRejects from '$lib/components/DnaRejects.svelte';
  import LedgerEditor from '$lib/components/LedgerEditor.svelte';
</script>

<h1 class="large-title">Corrections</h1>
<p class="lead footnote">
  Fixes made here are kept as the household's own rows, beside the bundle's, so they survive every
  re-import.
</p>

<div class="page">
  <section aria-label="DNA review">
    <DnaRejects />
  </section>

  <section class="group" aria-labelledby="editors">
    <h2 class="list-header" id="editors">Editors</h2>
    <div class="list-group editors">
      <LedgerEditor ledger="adjudications" />
      <LedgerEditor ledger="corrections" />
    </div>
  </section>
</div>

<style>
  h1 {
    padding-top: 8px;
  }
  .lead {
    margin: 8px 0 24px;
    padding: 0 4px;
  }
  .page {
    display: flex;
    flex-direction: column;
    gap: 32px;
  }
  .group {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .editors > :global(details + details) {
    box-shadow: inset 0 0.5px 0 var(--separator);
  }
</style>
