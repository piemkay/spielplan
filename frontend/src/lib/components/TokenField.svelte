<script>
  // A desktop Filters field (boards B1 to B4, B8 and C2): what is chosen as chips, then the words
  // that find more. A press or typing opens its list; focus alone does not, since a closing popover
  // hands focus back to the field.
  let {
    value = $bindable(''),
    field = $bindable(),
    input = $bindable(),
    label,
    placeholder,
    testid = undefined,
    inputTestid,
    expanded = false,
    controls,
    onpress,
    oninput,
    onkeydown = undefined,
    chips = undefined
  } = $props();

  /** @param {MouseEvent} event */
  function press(event) {
    // A chip's own buttons flip or remove it; they open nothing.
    if (event.target instanceof Element && event.target.closest('button')) return;
    input?.focus();
    onpress?.();
  }
</script>

<!-- The input takes the keys; a press on the field around it only puts the caret there. -->
<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
<div class="tokens" bind:this={field} data-testid={testid} onclick={press}>
  {@render chips?.()}
  <input
    type="text"
    bind:this={input}
    bind:value
    role="combobox"
    aria-label={label}
    aria-expanded={expanded}
    aria-controls={controls}
    aria-autocomplete="list"
    autocomplete="off"
    spellcheck="false"
    {placeholder}
    data-testid={inputTestid}
    {oninput}
    {onkeydown}
  />
</div>

<style>
  .tokens {
    min-width: 0;
    min-height: 40px;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
    padding: 4px;
    border-radius: var(--r-sm);
    background: var(--surface-2);
    cursor: text;
  }
  .tokens:focus-within {
    outline: 2px solid var(--accent-text);
    outline-offset: 2px;
  }
  /* Outranks design.css's field rule: the box draws the field, the input only holds the words. */
  .tokens > input[type='text'][aria-label] {
    flex: 1;
    width: auto;
    min-width: 96px;
    height: 32px;
    min-height: 32px;
    padding: 0 8px;
    border-radius: 0;
    background: none;
    outline: none;
    font-size: var(--fs-subhead);
    line-height: 20px;
  }
  /* A chip in the field is 32 px, as the boards draw it, not the chip row's 36. */
  .tokens :global(span.fchip),
  .tokens :global(.fchip button.body),
  .tokens :global(span.rchip:not(.parts)) {
    height: 32px;
    min-height: 32px;
  }
  .tokens :global(.fchip.person .face) {
    --face: 26px;
  }
  .tokens :global(.rchip .thumb) {
    width: 16px;
  }
</style>
