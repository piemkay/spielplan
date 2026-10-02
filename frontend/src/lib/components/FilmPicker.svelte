<script>
  // Like these films' title picker (decision 559 item 1, board C2): any title of either kind,
  // owned or not, that carries two or more taste terms; each joins the recipe liked or less liked.
  // On a phone a sheet; on a desktop the Filters cell is its field, the recipe's films its chips,
  // and the list drops under it.
  import { get, qs } from '$lib/api.js';
  import { FULL, MAX_FILMS, addFilm, recipe, removeFilm, setSign } from '$lib/recipe.svelte.js';
  import Icon from './Icon.svelte';
  import Popover from './Popover.svelte';
  import RatePoster from './RatePoster.svelte';
  import RecipeChip from './RecipeChip.svelte';
  import Sheet from './Sheet.svelte';
  import TokenField from './TokenField.svelte';

  let { open = false, inline = false, onClose = undefined } = $props();
  const listId = $props.id();

  let q = $state('');
  let found = $state([]);
  // The words `found` answers, so "No film found" waits for the answer.
  let answered = $state('');
  let error = $state('');
  let active = $state(0);
  // The inline drop, opened by a press or by typing.
  let dropped = $state(false);
  let field = $state();

  const films = $derived(recipe.films);
  const full = $derived(films.length >= MAX_FILMS);
  const query = $derived(q.trim());
  const listed = $derived(
    inline ? dropped && Boolean(query) && (found.length > 0 || answered === query || full || Boolean(error)) : open
  );

  let seq = 0;
  let timer;
  $effect(() => {
    const words = query;
    clearTimeout(timer);
    if (!words) {
      seq += 1;
      found = [];
      error = '';
      return;
    }
    timer = setTimeout(() => find(words), 220);
    return () => clearTimeout(timer);
  });

  async function find(words) {
    const mine = ++seq;
    try {
      const res = await get(`/mix/films${qs({ q: words, limit: 8 })}`);
      if (mine !== seq) return;
      found = res.items ?? [];
      answered = words;
      error = '';
      active = 0;
    } catch (err) {
      if (mine === seq) error = err.message;
    }
  }

  const signOf = (id) => films.find((f) => f.id === id)?.sign ?? null;

  // A pressed Like or Less like takes the film back out.
  function choose(film, sign) {
    if (signOf(film.id) === sign) return removeFilm(film.id);
    error = addFilm(film, sign) ?? '';
  }

  // The desktop field's keys; a phone's search key only searches, with no row highlighted to take.
  function onKey(event) {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      dropped = true;
      const step = event.key === 'ArrowDown' ? 1 : -1;
      active = Math.max(0, Math.min(found.length - 1, active + step));
    } else if (event.key === 'Enter' && listed && found[active]) {
      event.preventDefault();
      choose(found[active], event.shiftKey ? 'less' : 'like');
    }
  }

  function meta(film) {
    const where = film.is_owned ? 'In your library' : 'Not in the library';
    return [film.year, film.kind === 'series' ? 'Series' : null, where].filter(Boolean).join(' · ');
  }
</script>

{#snippet search()}
  <label class="find">
    <Icon name="search" size={18} />
    <input
      type="search"
      bind:value={q}
      autocomplete="off"
      enterkeyhint="search"
      aria-label="Find a film to like"
      placeholder="Add a film"
      data-testid="film-search"
    />
  </label>
{/snippet}

{#snippet chips()}
  {#each films as f (f.id)}
    <RecipeChip
      film={f}
      onFlip={() => setSign(f.id, f.sign === 'like' ? 'less' : 'like')}
      onRemove={() => removeFilm(f.id)}
    />
  {/each}
{/snippet}

{#snippet list()}
  {#if full}
    <p class="footnote note" role="status" data-testid="film-full">{FULL}</p>
  {:else if error}
    <p class="footnote note" role="alert">{error}</p>
  {/if}
  {#if found.length}
    {#if inline}<div class="colhead" aria-hidden="true">Films</div>{/if}
    <div class:list-group={!inline} class="rows">
      {#each found as film, i (film.id)}
        {@const sign = signOf(film.id)}
        <div class="row" class:active={inline && i === active} data-testid="film-row">
          <span class="thumb" aria-hidden="true"><RatePoster title={film} showName={false} /></span>
          <span class="text">
            <span class="name">{film.name}</span>
            <span class="meta">{meta(film)}</span>
          </span>
          <span class="acts" role="group" aria-label="{film.name}, {film.year}">
            <button
              class="act press"
              aria-pressed={sign === 'like'}
              disabled={full && !sign}
              onclick={() => choose(film, 'like')}>Like</button
            >
            <button
              class="act press"
              aria-pressed={sign === 'less'}
              disabled={full && !sign}
              onclick={() => choose(film, 'less')}>Less like</button
            >
          </span>
        </div>
      {/each}
    </div>
  {:else if !query}
    <p class="footnote note">Type the name of a film.</p>
  {:else if answered === query}
    <p class="footnote note">No film found for {query}.</p>
  {/if}
  {#if inline && found.length}
    <p class="footnote note">
      Enter likes the highlighted film, Shift+Enter makes it less like.{films.length ? ' Tap a chip to switch.' : ''}
    </p>
  {/if}
{/snippet}

{#if inline}
  <TokenField
    bind:value={q}
    bind:field
    label="Find a film to like"
    placeholder="Add a film"
    inputTestid="film-search"
    expanded={listed}
    controls={listId}
    onpress={() => (dropped = true)}
    oninput={() => (dropped = true)}
    onkeydown={onKey}
    {chips}
  />
  <Popover open={listed} anchor={field} label="Films to like or less like" width={640} onClose={() => (dropped = false)}>
    <div class="drop" id={listId}>{@render list()}</div>
  </Popover>
{:else}
  <Sheet {open} {onClose} label="Like these films">
    {#snippet header(close)}
      <div class="sheethead">
        <h2 class="section-title">Like these films</h2>
        <button class="btn-plain done" onclick={close}>Done</button>
      </div>
      <div class="top">
        {@render search()}
        {#if films.length}
          <div class="chosen" role="group" aria-label="In the recipe">{@render chips()}</div>
          <p class="footnote hint">Tap a chip to switch between like and less like.</p>
        {/if}
      </div>
    {/snippet}
    {#snippet children()}{@render list()}{/snippet}
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
  .chosen {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .hint {
    margin: -4px 4px 0;
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
  .note {
    margin: 0;
    padding: 8px 4px;
  }
  .row {
    display: flex;
    align-items: center;
    gap: 12px;
    min-height: 64px;
    padding: 8px 10px 8px 12px;
  }
  .list-group .row + .row {
    box-shadow: inset 0 0.5px 0 var(--separator);
  }
  .thumb {
    flex: none;
    width: 28px;
    border-radius: 3px;
    overflow: hidden;
  }
  .thumb :global(.poster) {
    border-radius: 0;
  }
  .text {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .name {
    overflow-wrap: anywhere;
    font-size: var(--fs-body);
    line-height: 22px;
  }
  .meta {
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-3);
    font-variant-numeric: tabular-nums;
  }
  .acts {
    flex: none;
    display: flex;
    gap: 6px;
  }
  .act {
    position: relative;
    display: inline-flex;
    align-items: center;
    height: 32px;
    min-height: 32px;
    padding: 0 10px;
    border: none;
    border-radius: var(--r-pill);
    background: var(--surface-3);
    color: var(--text);
    font-size: var(--fs-footnote);
    line-height: 18px;
    font-weight: 600;
    white-space: nowrap;
  }
  .act[aria-pressed='true'] {
    background: var(--text);
    color: var(--bg);
  }
  .act:disabled {
    opacity: 0.45;
    cursor: default;
  }
  /* 32 drawn, 48 to tap. */
  .act::after {
    content: '';
    position: absolute;
    inset: -8px -3px;
  }

  /* The desktop drop: rows under the cell's field, one highlighted for Enter. */
  .drop {
    display: flex;
    flex-direction: column;
    padding: 6px;
  }
  .colhead {
    padding: 6px 8px 4px;
    font-size: var(--fs-footnote);
    line-height: 18px;
    letter-spacing: 0.02em;
    text-transform: uppercase;
    color: var(--text-3);
  }
  .drop .row {
    min-height: 48px;
    padding: 4px 8px;
    border-radius: var(--r-sm);
  }
  .drop .row.active {
    background: var(--surface-2);
  }
  .drop .thumb {
    width: 24px;
  }
  .drop .text {
    flex-direction: row;
    align-items: baseline;
    gap: 10px;
  }
  .drop .name {
    flex: 0 1 auto;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: var(--fs-subhead);
    line-height: 20px;
  }
  .drop .meta {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .drop .note {
    padding: 6px 8px;
  }
</style>
