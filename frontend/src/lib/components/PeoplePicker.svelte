<script>
  // People (decision 557, board B4): a word-start typeahead over names, one person folded across
  // their records; a person is included in any role and never left out. A sheet on a phone; on a
  // desktop a popover under the Filters cell that opened it.
  import { searchPeople } from '$lib/filters.svelte.js';
  import FilterChip from './FilterChip.svelte';
  import Headshot from './Headshot.svelte';
  import Icon from './Icon.svelte';
  import Popover from './Popover.svelte';
  import Sheet from './Sheet.svelte';

  let {
    open = false,
    kinds = ['movie'],
    chosen = [],
    onAdd,
    onRemove,
    onClose,
    anchor = null,
    combinedLine = ''
  } = $props();

  const DEBOUNCE_MS = 220;
  const ROLES = {
    cast: 'Actor',
    director: 'Director',
    writer: 'Writer',
    dp: 'Cinematographer',
    composer: 'Composer',
    editor: 'Editor',
    prod_designer: 'Production designer'
  };

  let width = $state(typeof window === 'undefined' ? 390 : window.innerWidth);
  const desktop = $derived(Boolean(anchor) && width > 720);

  let q = $state('');
  let found = $state([]);
  // The query `found` answers; empty while nothing has been asked.
  let asked = $state('');
  let field = $state();
  let timer;
  let seq = 0;

  $effect(() => {
    if (!open) return;
    q = '';
    found = [];
    asked = '';
  });

  $effect(() => {
    if (open && desktop && field) field.focus({ preventScroll: true });
  });

  function typed() {
    clearTimeout(timer);
    const query = q.trim();
    const mine = ++seq;
    if (query.length < 2) {
      found = [];
      asked = '';
      return;
    }
    timer = setTimeout(async () => {
      const people = await searchPeople(query, kinds).catch(() => []);
      if (mine !== seq) return;
      found = people;
      asked = query;
    }, DEBOUNCE_MS);
  }

  const added = (p) => chosen.some((c) => c.person_ids.some((id) => p.person_ids.includes(id)));

  function add(p) {
    if (added(p)) return;
    onAdd?.(p);
    clearTimeout(timer);
    seq++;
    q = '';
    found = [];
    asked = '';
    field?.focus({ preventScroll: true });
  }
</script>

<svelte:window bind:innerWidth={width} />

{#snippet top()}
  <label class="find">
    <Icon name="search" size={18} />
    <input
      type="search"
      bind:this={field}
      bind:value={q}
      oninput={typed}
      autocomplete="off"
      enterkeyhint="search"
      aria-label="Find a person"
      placeholder="Search people"
      data-testid="people-search"
    />
  </label>
  {#if chosen.length}
    <div class="chosen" role="group" aria-label="Chosen people">
      {#each chosen as p (p.person_ids.join(','))}
        <FilterChip variant="person" label={p.name} person={p} testid="picker-person-chip" onRemove={() => onRemove?.(p)} />
      {/each}
    </div>
  {/if}
  {#if combinedLine}<p class="footnote combined" data-testid="people-combined">{combinedLine}</p>{/if}
{/snippet}

{#snippet list()}
  {#if found.length}
    <div class="results" class:list-group={!desktop}>
      {#each found as p (p.person_id)}
        {@const done = added(p)}
        <button
          class="row"
          disabled={done}
          aria-label={done ? `${p.name}, added` : `Add ${p.name}`}
          onclick={() => add(p)}
          data-testid="person-row"
        >
          <span class="face"><Headshot credit={p} /></span>
          <span class="text">
            <span class="name">{p.name}</span>
            <span class="meta">{[ROLES[p.role], `${(p.owned ?? 0).toLocaleString()} in your library`].filter(Boolean).join(' · ')}</span>
          </span>
          <span class="add" aria-hidden="true">{done ? 'Added' : 'Add'}</span>
        </button>
      {/each}
    </div>
  {:else if asked}
    <p class="none" data-testid="people-none">No one matches {asked}</p>
  {/if}
{/snippet}

{#if desktop}
  <Popover {open} {anchor} label="People" {onClose}>
    <div class="picker desktop">
      <div class="top">{@render top()}</div>
      <div class="list">{@render list()}</div>
    </div>
  </Popover>
{:else}
  <Sheet {open} {onClose} label="People">
    {#snippet header(close)}
      <div class="sheethead">
        <h2 class="section-title">People</h2>
        <button class="btn-plain done" onclick={close}>Done</button>
      </div>
      <div class="top">{@render top()}</div>
    {/snippet}
    {#snippet children()}
      {@render list()}
    {/snippet}
  </Sheet>
{/if}

<style>
  .sheethead {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    min-height: 44px;
  }
  .done {
    margin-right: -8px;
    font-weight: 600;
  }
  .top {
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 8px 0 12px;
  }
  .find {
    display: flex;
    align-items: center;
    gap: 8px;
    padding-left: 12px;
    border-radius: var(--r-sm);
    background: var(--surface-2);
    color: var(--text-3);
    cursor: text;
  }
  .find:focus-within {
    outline: 2px solid var(--accent-text);
    outline-offset: 2px;
  }
  /* Outranks design.css's field rule: the label draws the field, the input only holds the text. */
  .find > input[type='search'][aria-label] {
    flex: 1;
    min-width: 0;
    min-height: 48px;
    padding: 0 12px 0 0;
    border-radius: 0;
    background: none;
    outline: none;
  }
  .chosen {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .combined {
    margin: -4px 4px 0;
  }
  .results {
    display: flex;
    flex-direction: column;
  }
  .row {
    display: flex;
    align-items: center;
    gap: 12px;
    width: 100%;
    min-height: 64px;
    padding: 10px 12px 10px 16px;
    border: none;
    background: none;
    color: var(--text);
    text-align: left;
  }
  .row + .row {
    box-shadow: inset 0 0.5px 0 var(--separator);
  }
  .row:disabled {
    cursor: default;
  }
  .face {
    --face: 40px;
    flex: none;
    display: flex;
  }
  .text {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: var(--fs-body);
    line-height: 22px;
  }
  .meta {
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-3);
  }
  .add {
    flex: none;
    display: inline-flex;
    align-items: center;
    height: 32px;
    padding: 0 12px;
    border-radius: var(--r-pill);
    background: var(--surface-3);
    font-size: var(--fs-footnote);
    line-height: 18px;
    font-weight: 600;
  }
  .row:disabled .add {
    background: none;
    color: var(--text-3);
  }
  .none {
    margin: 0;
    padding: 16px 4px;
    color: var(--text-2);
  }

  .picker.desktop {
    flex: 1 1 auto;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }
  .desktop .top {
    flex: none;
    padding: 12px 12px 8px;
  }
  .desktop .find > input[type='search'][aria-label] {
    min-height: 44px;
  }
  .desktop .list {
    flex: 1 1 auto;
    min-height: 0;
    overflow-y: auto;
    padding: 0 6px 6px;
  }
  .desktop .none {
    padding: 8px 10px 12px;
  }
  .desktop .row {
    min-height: 52px;
    padding: 0 8px 0 10px;
    border-radius: var(--r-sm);
  }
  .desktop .row + .row {
    box-shadow: none;
  }
  @media (hover: hover) {
    .desktop .row:not(:disabled):hover {
      background: var(--surface-2);
    }
  }
  .desktop .face {
    --face: 32px;
  }
  .desktop .name {
    font-size: var(--fs-subhead);
    line-height: 20px;
  }
</style>
