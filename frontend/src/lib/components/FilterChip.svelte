<script>
  // A set filter as a chip (decision 557, board B7). A term's body flips it between include and
  // leave out and its x removes it; a person is only ever included; any other filter is one button.
  import { facetColour } from '$lib/home.svelte.js';
  import Headshot from './Headshot.svelte';

  let {
    variant = 'plain', mode = 'in', label, facet = '', person = null, onFlip = undefined, onRemove, testid
  } = $props();

  const out = $derived(variant === 'term' && mode === 'out');
</script>

{#snippet x()}
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" aria-hidden="true">
    <path d="M6 6l12 12M18 6 6 18" />
  </svg>
{/snippet}

{#if variant === 'plain'}
  <button class="pill on plain" data-testid={testid} aria-label="Remove {label}" onclick={onRemove}>
    <span class="label">{label}</span><span class="glyph">{@render x()}</span>
  </button>
{:else}
  <span class="fchip {variant}" class:out data-testid={testid} data-mode={variant === 'term' ? mode : undefined}>
    {#if variant === 'term'}
      <button
        class="body"
        aria-label="Switch {label} to {out ? 'include' : 'leave out'}"
        onclick={onFlip}
      >
        <span class="dot" style:background={facetColour(facet)} aria-hidden="true"></span>
        <span class="label">{#if out}<span aria-hidden="true">{'− '}</span>{/if}{label}</span>
      </button>
    {:else}
      <span class="face"><Headshot credit={person} /></span>
      <span class="label">{label}</span>
    {/if}
    <button class="x" aria-label="Remove {label}" onclick={onRemove}>{@render x()}</button>
  </span>
{/if}

<style>
  .fchip {
    position: relative;
    flex: none;
    display: inline-flex;
    align-items: center;
    max-width: 100%;
    height: 36px;
    padding: 0 2px 0 0;
    border-radius: var(--r-pill);
    background: var(--text);
    color: var(--bg);
    font-size: var(--fs-subhead);
    line-height: 20px;
    font-weight: 600;
  }
  .fchip.out {
    background: none;
    box-shadow: inset 0 0 0 1px rgba(245, 240, 232, 0.42);
    color: var(--text);
  }
  .fchip.person {
    gap: 8px;
    padding-left: 4px;
  }
  .label {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .body {
    position: relative;
    min-width: 0;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    height: 36px;
    min-height: 36px;
    padding: 0 2px 0 14px;
    border: none;
    border-radius: var(--r-pill) 0 0 var(--r-pill);
    background: none;
    color: inherit;
    font: inherit;
  }
  .dot {
    flex: none;
    width: 6px;
    height: 6px;
    border-radius: var(--r-pill);
  }
  .out .dot {
    opacity: 0.45;
  }
  .face {
    --face: 28px;
    flex: none;
    display: flex;
  }
  .x {
    position: relative;
    flex: none;
    display: grid;
    place-items: center;
    width: 32px;
    height: 32px;
    min-height: 32px;
    padding: 0;
    border: none;
    border-radius: var(--r-pill);
    background: none;
    color: rgba(12, 11, 10, 0.55);
  }
  .out .x {
    color: var(--text-2);
  }
  /* 36 drawn, 48 to tap: the body reaches up and down, the x 44 by 48 (board B7). */
  .body::after,
  .x::after {
    content: '';
    position: absolute;
    inset: -6px 0 -6px -2px;
  }
  .x::after {
    inset: -8px -6px;
  }
  .plain {
    max-width: 100%;
    gap: 2px;
    padding: 0 2px 0 14px;
  }
  .glyph {
    flex: none;
    display: grid;
    place-items: center;
    width: 32px;
    height: 32px;
    color: rgba(12, 11, 10, 0.55);
  }
</style>
