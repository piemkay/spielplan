<script>
  // A recipe film as a chip (decision 560, board MA): the body opens the film's sheet, the x
  // removes the film. Like is filled, Less like outlined; a film lending groups shows their dots
  // and terms on a second line.
  import { chipLabel, chipTerms, groupOf } from '$lib/recipe.svelte.js';
  import RatePoster from './RatePoster.svelte';

  let { film, expanded = false, onOpen, onRemove } = $props();

  let body = $state();
  const label = $derived(chipLabel(film));
  const terms = $derived(chipTerms(film));
  const parts = $derived(film.groups.length > 0);
</script>

{#snippet glyph(name)}
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d={name === 'x' ? 'M6 6l12 12M18 6 6 18' : 'm6 9.5 6 6 6-6'} /></svg>
{/snippet}

<span class="rchip" class:less={film.sign === 'less'} class:parts data-testid="recipe-chip">
  <button
    class="body"
    bind:this={body}
    aria-haspopup="dialog"
    aria-expanded={expanded}
    aria-label={`${label}${terms ? `: ${terms}` : ''}. Choose like, less like, or ${parts ? 'other parts' : 'only parts of it'}`}
    onclick={() => onOpen(body)}
  >
    <span class="thumb" aria-hidden="true"><RatePoster title={{ title_id: film.id, name: film.name }} showName={false} /></span>
    <span class="text">
      <span class="label">{label}</span>
      {#if parts}
        <span class="sub">
          <span class="dots" aria-hidden="true">
            {#each film.groups as g (g)}<span class="dot" style:background={groupOf(g).colour}></span>{/each}
          </span>
          <span class="terms">{terms}</span>
        </span>
      {/if}
    </span>
    {@render glyph('chevron')}
  </button>
  <button class="x" aria-label="Remove {film.name || 'this film'}" onclick={onRemove}>{@render glyph('x')}</button>
</span>

<style>
  .rchip {
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
  }
  .rchip.parts {
    height: 50px;
  }
  .rchip.less {
    background: none;
    box-shadow: inset 0 0 0 1px rgba(245, 240, 232, 0.42);
    color: var(--text);
  }
  .body {
    position: relative;
    min-width: 0;
    display: inline-flex;
    align-items: center;
    gap: 8px;
    height: 100%;
    min-height: 0;
    padding: 0 2px 0 5px;
    border: none;
    border-radius: var(--r-pill) 0 0 var(--r-pill);
    background: none;
    color: inherit;
    font-size: var(--fs-subhead);
    line-height: 20px;
    font-weight: 600;
    text-align: left;
  }
  .parts .body {
    padding-left: 6px;
  }
  .thumb {
    flex: none;
    width: 18px;
    border-radius: 3px;
    overflow: hidden;
  }
  .less .thumb {
    opacity: 0.5;
  }
  .thumb :global(.poster) {
    border-radius: 0;
  }
  .text {
    min-width: 0;
    display: flex;
    flex-direction: column;
  }
  .label {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .sub {
    min-width: 0;
    max-width: 196px;
    display: flex;
    align-items: center;
    gap: 5px;
    font-size: var(--fs-footnote);
    line-height: 16px;
    font-weight: 500;
    color: rgba(12, 11, 10, 0.62);
  }
  .less .sub {
    color: var(--text-3);
  }
  .dots {
    flex: none;
    display: inline-flex;
    gap: 3px;
  }
  .dot {
    width: 8px;
    height: 8px;
    border-radius: var(--r-pill);
  }
  .terms {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .body svg {
    flex: none;
    opacity: 0.55;
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
  .less .x {
    color: var(--text-2);
  }
  /* 36 drawn, 48 to tap (board B7). */
  .body::after,
  .x::after {
    content: '';
    position: absolute;
    inset: -6px 0 -6px -2px;
  }
  .x::after {
    inset: -8px -6px;
  }
  .parts .body::after,
  .parts .x::after {
    inset: 0;
  }
</style>
