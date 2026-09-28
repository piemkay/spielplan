<script>
  // Inputs as one grouped list, each row a real <label>: its words, then the field (decision 527).
  let { children } = $props();
</script>

<div class="fields">{@render children()}</div>

<style>
  .fields {
    display: grid;
    grid-template-columns: minmax(88px, max-content) minmax(0, 1fr);
    column-gap: 12px;
    border-radius: var(--r-md);
    background: var(--surface-1);
    overflow: hidden;
  }
  /* One label column for every row, as wide as the longest label. */
  .fields > :global(label) {
    position: relative;
    grid-column: 1 / -1;
    display: grid;
    grid-template-columns: 88px minmax(0, 1fr);
    grid-template-columns: subgrid;
    align-items: center;
    min-height: 52px;
    padding: 0 var(--gutter);
    font-size: var(--fs-body);
    line-height: 22px;
  }
  .fields > :global(label + label::before) {
    content: '';
    position: absolute;
    top: 0;
    left: var(--gutter);
    right: 0;
    height: 0.5px;
    background: var(--separator);
  }
  /* design.css draws every text input as a filled box; here the row is the field. `#_` lifts this
     rule over that one's four `:not()`s. */
  .fields :global(input:not(#_)) {
    min-height: 52px;
    padding: 0;
    border-radius: 0;
    background: none;
  }
</style>
