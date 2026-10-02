<script>
  // A recipe film's sheet (decision 560, boards MA-2 and MA-5): All of the film, or any of its
  // groups, each listing the film's terms there, with one Like or Less like for the film. Nothing
  // changes until Apply. A sheet on a phone; on a desktop a popover under the chip.
  import { untrack } from 'svelte';
  import {
    GROUPS,
    chipLabel,
    chipTerms,
    groupNote,
    groupRow,
    lendNote,
    setGroups,
    setSign
  } from '$lib/recipe.svelte.js';
  import Popover from './Popover.svelte';
  import RatePoster from './RatePoster.svelte';
  import Sheet from './Sheet.svelte';

  let { open = false, film = null, films = [], anchor = null, onClose } = $props();

  // Our read's terms past these wait behind "Show N more".
  const INFERRED = 4;

  let width = $state(typeof window === 'undefined' ? 390 : window.innerWidth);
  const desktop = $derived(Boolean(anchor) && width > 720);

  let sel = $state([]);
  let sign = $state('like');
  let more = $state([]);

  const filmId = $derived(film?.id ?? null);
  $effect.pre(() => {
    if (!open || filmId === null) return;
    untrack(() => {
      sel = [...film.groups];
      sign = film.sign;
      more = [];
    });
  });

  const staged = $derived(film ? { ...film, groups: sel, sign } : null);
  const rows = $derived(
    film?.sheet ? GROUPS.map((g) => film.sheet.find((r) => r.group === g.key)).filter(Boolean) : []
  );
  const meta = $derived(
    [film?.year, film?.is_owned === true ? 'In your library' : film?.is_owned === false ? 'Not in your library' : null]
      .filter(Boolean)
      .join(' · ')
  );

  function toggle(key) {
    sel = sel.includes(key) ? sel.filter((g) => g !== key) : [...sel, key];
  }

  function apply(close) {
    setGroups(film.id, sel);
    setSign(film.id, sign);
    close();
  }
</script>

<svelte:window bind:innerWidth={width} />

{#snippet head(close)}
  <div class="head">
    <span class="thumb" aria-hidden="true"><RatePoster title={{ title_id: film.id, name: film.name }} showName={false} /></span>
    <span class="text">
      <span class="label" data-testid="recipe-sheet-label">{chipLabel(staged)}</span>
      <span class="sub">{sel.length ? chipTerms(staged) : meta}</span>
    </span>
    <div class="segmented sign" role="group" aria-label="Like or less like">
      <button aria-pressed={sign === 'like'} onclick={() => (sign = 'like')}>Like</button>
      <button aria-pressed={sign === 'less'} onclick={() => (sign = 'less')}>Less like</button>
    </div>
    <button class="btn-plain apply" onclick={() => apply(close)}>Apply</button>
  </div>
  <p class="fx" data-testid="recipe-sheet-note">{groupNote(film, sel, sign, films)}</p>
{/snippet}

{#snippet chips(terms, inferred)}
  <span class="terms">
    {#each terms as t (t.term)}<span class="term" class:inferred>{t.label}</span>{/each}
  </span>
{/snippet}

{#snippet body()}
  <div class="takehead">
    <h3>Take from {film.name}</h3>
    <span>Pick one or more</span>
  </div>
  <div class="rows" class:card={!desktop} role="group" aria-label="What to take from {film.name}">
    <button class="row all" role="checkbox" aria-checked={sel.length === 0} onclick={() => (sel = [])}>
      <span class="thumb small" aria-hidden="true"><RatePoster title={{ title_id: film.id, name: film.name }} showName={false} /></span>
      <span class="name">All of {film.name}</span>
      <span class="preview">The whole film, every part</span>
      <span class="radio" aria-hidden="true"></span>
    </button>
    {#if !rows.length}
      <p class="footnote wait">Reading {film.name}'s terms…</p>
    {/if}
    {#each rows as r (r.group)}
      {@const state = groupRow(film, r, films)}
      {@const on = sel.includes(r.group)}
      {@const all = [...r.quoted, ...r.inferred]}
      {@const colour = GROUPS.find((g) => g.key === r.group).colour}
      <div class="group" class:on data-testid="recipe-group">
        <button
          class="row"
          role="checkbox"
          aria-checked={on}
          aria-disabled={state.disabled}
          onclick={() => !state.disabled && toggle(r.group)}
        >
          <span class="dot" style:--c={colour} aria-hidden="true"></span>
          <span class="name">{r.name}</span>
          <span class="preview">
            {#if state.reason}<span class="reason">{state.reason}</span>{:else if !on}{all.map((t) => t.label).join(', ')}{/if}
          </span>
          <span class="box" aria-hidden="true">
            {#if on}<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12.5 4.5 4.5L19 7.5" /></svg>{/if}
          </span>
        </button>
        {#if on}
          <div class="detail">
            {#if r.quoted.length}{@render chips(r.quoted, false)}{/if}
            {#if r.inferred.length}
              {@const shown = more.includes(r.group) ? r.inferred : r.inferred.slice(0, INFERRED)}
              <p class="ours">Our read · less certain</p>
              {@render chips(shown, true)}
              {#if shown.length < r.inferred.length}
                <button class="btn-plain more" onclick={() => (more = [...more, r.group])}>
                  Show {r.inferred.length - shown.length} more
                </button>
              {/if}
            {/if}
          </div>
        {/if}
      </div>
    {/each}
  </div>
  <p class="footnote foot">{lendNote(film, films)}</p>
{/snippet}

{#if film}
  {#if desktop}
    <Popover {open} {anchor} label="{film.name} in your recipe" width={480} {onClose}>
      <div class="sheet desktop">
        {@render head(onClose)}
        {@render body()}
      </div>
    </Popover>
  {:else}
    <Sheet {open} {onClose} label="{film.name} in your recipe">
      {#snippet header(close)}{@render head(close)}{/snippet}
      {#snippet children()}{@render body()}{/snippet}
    </Sheet>
  {/if}
{/if}

<style>
  .head {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    grid-template-areas: 'thumb text apply' 'sign sign sign';
    align-items: center;
    gap: 12px 10px;
    padding: 6px 0 4px;
  }
  .desktop .head {
    grid-template-columns: auto minmax(0, 1fr) 176px auto;
    grid-template-areas: 'thumb text sign apply';
    padding: 6px 4px 8px 10px;
  }
  .head > .thumb {
    grid-area: thumb;
    width: 24px;
  }
  .head > .text {
    grid-area: text;
    min-width: 0;
    display: flex;
    flex-direction: column;
  }
  .label {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: var(--fs-subhead);
    line-height: 20px;
    font-weight: 600;
  }
  .sub {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-3);
    font-variant-numeric: tabular-nums;
  }
  .sign {
    grid-area: sign;
  }
  .apply {
    grid-area: apply;
    min-height: 36px;
    padding: 0 8px;
    font-weight: 600;
  }
  .fx {
    margin: 0;
    padding: 4px 0 12px;
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-2);
    text-wrap: pretty;
  }
  .desktop .fx {
    padding: 0 10px 10px;
  }
  .thumb {
    flex: none;
    border-radius: 3px;
    overflow: hidden;
  }
  .thumb :global(.poster) {
    border-radius: 0;
  }
  .thumb.small {
    width: 16px;
  }
  .takehead {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
    padding: 8px 0 4px;
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-3);
  }
  .desktop .takehead {
    padding: 8px 10px 4px;
    box-shadow: inset 0 0.5px 0 var(--separator);
  }
  .takehead h3 {
    margin: 0;
    font: inherit;
    letter-spacing: 0.02em;
    text-transform: uppercase;
  }
  .rows {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .rows.card {
    border-radius: var(--r-md);
    background: var(--surface-1);
  }
  .desktop .rows {
    padding: 0 6px;
  }
  .group.on {
    border-radius: var(--r-sm);
    background: var(--surface-2);
  }
  .row {
    width: 100%;
    min-height: 48px;
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 6px 10px;
    border: none;
    border-radius: var(--r-sm);
    background: none;
    color: var(--text);
    text-align: left;
  }
  .all[aria-checked='true'] {
    background: var(--surface-2);
  }
  .row[aria-disabled='true'] {
    cursor: default;
  }
  .name {
    flex: none;
    width: 104px;
    font-size: var(--fs-subhead);
    line-height: 20px;
    font-weight: 500;
  }
  .all .name {
    width: auto;
    max-width: 50%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .row[aria-disabled='true'] .name {
    color: var(--text-3);
  }
  .preview {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-2);
  }
  .reason {
    color: var(--text-3);
  }
  .dot {
    flex: none;
    width: 10px;
    height: 10px;
    margin: 0 3px;
    border-radius: var(--r-pill);
    box-shadow: inset 0 0 0 1.5px var(--c);
  }
  .on .dot {
    background: var(--c);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--c) 28%, transparent);
  }
  [aria-disabled='true'] .dot {
    opacity: 0.45;
  }
  .box,
  .radio {
    flex: none;
    width: 22px;
    height: 22px;
    margin-left: auto;
    display: grid;
    place-items: center;
    border-radius: 6px;
    box-shadow: inset 0 0 0 1.5px rgba(245, 240, 232, 0.42);
    color: var(--bg);
  }
  [aria-disabled='true'] .box {
    box-shadow: inset 0 0 0 1.5px rgba(245, 240, 232, 0.18);
  }
  .on .box {
    background: var(--accent-text);
    box-shadow: none;
  }
  .radio {
    border-radius: var(--r-pill);
  }
  [aria-checked='true'] > .radio {
    box-shadow: inset 0 0 0 2px var(--accent-text);
  }
  [aria-checked='true'] > .radio::after {
    content: '';
    width: 10px;
    height: 10px;
    border-radius: var(--r-pill);
    background: var(--accent-text);
  }
  .detail {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 6px;
    padding: 0 10px 10px 36px;
  }
  .terms {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .term {
    display: inline-flex;
    align-items: center;
    height: 26px;
    padding: 0 10px;
    border-radius: var(--r-pill);
    background: var(--surface-3);
    font-size: var(--fs-footnote);
    line-height: 18px;
    white-space: nowrap;
  }
  .term.inferred {
    background: none;
    box-shadow: inset 0 0 0 1px rgba(245, 240, 232, 0.24);
    color: var(--text-2);
  }
  .ours {
    margin: 4px 0 0;
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-3);
  }
  .more {
    min-height: 32px;
    padding: 0 4px;
    font-size: var(--fs-footnote);
    font-weight: 600;
  }
  .wait,
  .foot {
    margin: 0;
    padding: 12px 4px 0;
  }
  .desktop .foot {
    padding: 10px 10px 8px;
  }
  .desktop {
    padding: 6px 0;
  }
</style>
