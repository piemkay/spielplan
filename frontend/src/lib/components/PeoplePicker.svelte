<script>
  // People (decision 557, board B4): a word-start typeahead over names, one person folded across
  // their records; a person is included in any role and never left out. A sheet on a phone; `inline`
  // on a desktop, where the Filters cell is its field and the matches drop under it.
  import { searchPeople } from '$lib/filters.svelte.js';
  import FilterChip from './FilterChip.svelte';
  import Headshot from './Headshot.svelte';
  import Icon from './Icon.svelte';
  import Popover from './Popover.svelte';
  import Sheet from './Sheet.svelte';
  import TokenField from './TokenField.svelte';

  let {
    open = false,
    kinds = ['movie'],
    chosen = [],
    onAdd,
    onRemove,
    onClose = undefined,
    combinedLine = '',
    inline = false,
    testid = 'filter-people'
  } = $props();

  const DEBOUNCE_MS = 220;
  const listId = $props.id();
  const ROLES = {
    cast: 'Actor',
    director: 'Director',
    writer: 'Writer',
    dp: 'Cinematographer',
    composer: 'Composer',
    editor: 'Editor',
    prod_designer: 'Production designer'
  };

  let q = $state('');
  let found = $state([]);
  // The query `found` answers; empty while nothing has been asked.
  let asked = $state('');
  // The inline field's matches, opened by a press or by typing; Enter adds the highlighted one.
  let listed = $state(false);
  let active = $state(0);
  let field = $state();
  let input = $state();
  let timer;
  let seq = 0;

  const showing = $derived(listed && (found.length > 0 || Boolean(asked)));

  $effect(() => {
    if (!open) return;
    q = '';
    found = [];
    asked = '';
    // A search still pending when it closes must not answer the next opening.
    return () => {
      clearTimeout(timer);
      seq++;
    };
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
      // A failed read is no answer: it must not say that no one matches.
      const people = await searchPeople(query, kinds).catch(() => null);
      if (mine !== seq) return;
      found = people ?? [];
      asked = people ? query : '';
      active = 0;
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
    input?.focus({ preventScroll: true });
  }

  function onKey(event) {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      listed = true;
      active = Math.max(0, Math.min(found.length - 1, active + (event.key === 'ArrowDown' ? 1 : -1)));
    } else if (event.key === 'Enter' && showing && found[active]) {
      event.preventDefault();
      add(found[active]);
    }
  }
</script>

{#snippet top()}
  <label class="find">
    <Icon name="search" size={18} />
    <input
      type="search"
      bind:this={input}
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

{#snippet fieldChips()}
  {#each chosen as p (p.person_ids.join(','))}
    <FilterChip variant="person" label={p.name} person={p} testid="person-chip" onRemove={() => onRemove?.(p)} />
  {/each}
{/snippet}

{#snippet list()}
  {#if found.length}
    <div class="results" class:list-group={!inline}>
      {#each found as p, i (p.person_id)}
        {@const done = added(p)}
        <button
          class="row"
          class:active={inline && i === active}
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

{#if inline}
  <TokenField
    bind:value={q}
    bind:field
    bind:input
    label="Find a person"
    placeholder="Add a person"
    {testid}
    inputTestid="people-search"
    expanded={showing}
    controls={listId}
    onpress={() => (listed = true)}
    oninput={() => {
      listed = true;
      typed();
    }}
    onkeydown={onKey}
    chips={fieldChips}
  />
  <Popover open={showing} anchor={field} label="People" width={null} onClose={() => (listed = false)}>
    <div class="picker desktop" id={listId}>{@render list()}</div>
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
    overflow-y: auto;
    padding: 6px;
  }
  .desktop .none {
    padding: 8px 10px;
  }
  .desktop .row {
    min-height: 52px;
    padding: 0 8px 0 10px;
    border-radius: var(--r-sm);
  }
  .desktop .row + .row {
    box-shadow: none;
  }
  .desktop .row.active {
    background: var(--surface-2);
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
