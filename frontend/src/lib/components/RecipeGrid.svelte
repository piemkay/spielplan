<script>
  // A recipe's grid (decisions 559 and 560): one read per kind, films first on Both. The library
  // comes first; the well-known films beyond it wait behind one fold while Only in library is on.
  import { untrack } from 'svelte';
  import { get, qs } from '$lib/api.js';
  import { plural } from '$lib/home.svelte.js';
  import { homeFilters } from '$lib/homeFilters.svelte.js';
  import { captions, recipe, remember, sentence, twistsOffered, whyLine } from '$lib/recipe.svelte.js';
  import PosterCard from './PosterCard.svelte';
  import TwistRow from './TwistRow.svelte';

  /** @type {{kinds?: string[], params?: Record<string, any>, onSelect?: (title: any) => void}} */
  let { kinds = ['movie'], params = {}, onSelect = undefined } = $props();

  const LIMIT = 60;
  const THIN = 10;
  const ORDERS = [
    { id: 'match', label: 'Best match' },
    { id: 'for_you', label: 'For you' }
  ];

  // The catalogue's filters with the recipe; the mix routes split the library from beyond it.
  const query = $derived.by(() => {
    const { owned: _owned, ...rest } = params;
    return { ...rest, like: [...homeFilters.like], less: [...homeFilters.less] };
  });
  const folded = $derived(params.owned === 'only');

  let sort = $state('match');
  let regions = $state([]);
  let loading = $state(true);
  let error = $state('');
  // The query the grid last asked with: the twist row follows it, not every keystroke.
  let asked = $state.raw(null);

  const films = $derived(recipe.films);
  const said = $derived(sentence(films));
  const read0 = $derived(regions[0]?.page ?? null);
  const asks = $derived(Boolean(read0?.recipe.asks_for_like));

  const key = $derived(JSON.stringify([kinds, query, sort, folded]));
  let seq = 0;
  // A typed search narrows the recipe as the catalogue's does: once the typing pauses.
  let typed = null;
  let timer;
  $effect(() => {
    key;
    untrack(() => {
      clearTimeout(timer);
      if (typed !== null && params.q !== typed) timer = setTimeout(load, 220);
      else load();
      typed = params.q;
    });
    return () => clearTimeout(timer);
  });

  const rowsIn = (cells) => cells.reduce((n, c) => n + (c.fold ? c.fold.items.length : 1), 0);

  function read(kind, pool, offset) {
    return get(
      `/mix/titles${qs({
        ...query,
        kind,
        pool: pool === 'beyond' ? pool : undefined,
        sort: sort === 'match' ? undefined : sort,
        limit: LIMIT,
        offset: offset || undefined
      })}`
    );
  }

  async function load() {
    const mine = ++seq;
    asked = query;
    loading = true;
    error = '';
    try {
      const pages = await Promise.all(kinds.map((kind) => read(kind, 'library', 0)));
      if (mine !== seq) return;
      for (const page of pages) {
        for (const film of page.recipe.ingredients) remember({ ...film, id: film.title_id });
      }
      regions = pages.map((page, i) => ({ kind: kinds[i], page, cells: page.items, beyond: null, weak: false, opened: [], busy: false }));
      if (!folded) regions.forEach((r, i) => r.page.beyond_total && openBeyond(i));
    } catch (err) {
      if (mine === seq) {
        error = err.message;
        regions = [];
      }
    } finally {
      if (mine === seq) loading = false;
    }
  }

  async function more(i) {
    const mine = seq;
    const r = regions[i];
    if (r.busy) return;
    r.busy = true;
    const page = await read(r.kind, 'library', rowsIn(r.cells)).catch(() => null);
    if (mine !== seq) return;
    r.busy = false;
    if (page) r.cells = [...r.cells, ...page.items];
  }

  async function openBeyond(i) {
    const mine = seq;
    const r = regions[i];
    r.beyond = { cells: r.beyond?.cells ?? [], total: r.page.beyond_total, busy: true };
    const page = await read(r.kind, 'beyond', rowsIn(r.beyond.cells)).catch(() => null);
    if (mine !== seq) return;
    r.beyond = { cells: [...r.beyond.cells, ...(page?.items ?? [])], total: page?.total ?? r.beyond.total, busy: false };
  }

  function countLine(r) {
    const n = r.page.library_total;
    if (!n) return `No ${plural(r.kind, 2)} in your library fit`;
    if (n < THIN) return `Only ${n} ${plural(r.kind, n)} in your library ${n === 1 ? 'fits' : 'fit'}`;
    return `${n.toLocaleString()} in your library`;
  }

  const strong = (c) => (c.fold ? c.fold.items[0]?.match : c.match) !== 'weak';
  const foldKey = (c) => `fold:${c.fold.person_ids.join(',')}`;
  const labels = (terms) => terms.map((t) => t.label).join(', ');

  function open(t) {
    onSelect?.({ ...t, whyLine: whyLine(t.why) });
  }
</script>

{#snippet card(t)}
  <!-- No "In library" badge: the grid heads its library and its beyond apart. -->
  <PosterCard title={{ ...t, is_owned: null }} captions={captions(t.why)} onSelect={() => open(t)} />
{/snippet}

{#snippet cells(r, list)}
  {#each list as c (c.fold ? foldKey(c) : c.id)}
    {#if !c.fold}
      {@render card(c)}
    {:else if r.opened.includes(foldKey(c))}
      {#each c.fold.items as t (t.id)}{@render card(t)}{/each}
    {:else}
      <button
        class="foldcell press"
        data-testid="recipe-fold"
        aria-expanded="false"
        aria-label="Show {c.fold.items.length} more by {c.fold.name}: {c.fold.items.map((t) => t.name).join(', ')}"
        onclick={() => r.opened.push(foldKey(c))}
      >
        <span class="plus">+{c.fold.items.length}</span>
        <span class="by">more by {c.fold.name}</span>
      </button>
    {/if}
  {/each}
{/snippet}

<div class="recipe" data-testid="home-mode" data-mode="grid" data-reason="recipe">
  <div class="head">
    {#if said}
      <p class="line" data-testid="recipe-sentence">{said}</p>
    {:else}
      {#if read0 && (read0.recipe.more.length || read0.recipe.less.length)}
        <p class="line" data-testid="recipe-derived">
          {#if read0.recipe.more.length}<span class="k">more:</span> {labels(read0.recipe.more)}{/if}
          {#if read0.recipe.more.length && read0.recipe.less.length}<span aria-hidden="true"> · </span>{/if}
          {#if read0.recipe.less.length}<span class="k">less:</span> {labels(read0.recipe.less)}{/if}
        </p>
      {/if}
      {#if films.length}
        <p class="line hint">Tap a film to take one or more parts, like its mood or look.</p>
      {/if}
    {/if}
  </div>

  {#if asked && twistsOffered(films) && !asks}
    <TwistRow kind={kinds[0]} query={asked} />
  {/if}

  {#if error}
    <div class="empty card"><p class="why">{error}</p></div>
  {:else if !regions.length}
    <div class="grid" aria-hidden="true">
      {#each { length: 9 }, i (i)}<span class="skeleton ghost"></span>{/each}
    </div>
  {:else if asks}
    <div class="empty card" data-testid="recipe-asks">
      <h2 class="section-title">Like a film to start</h2>
      <p class="why">Less like only pushes films away. Add a film you like, or take a part of one.</p>
    </div>
  {:else}
    <div class="regions" class:dim={loading} aria-busy={loading}>
      {#each regions as r, i (r.kind)}
        {@const page = r.page}
        {@const split = page.strong_total !== null && page.strong_total !== undefined}
        <section class="region" data-testid="recipe-region-{r.kind}">
          {#if kinds.length > 1}
            <h2 class="list-header kindhead">{r.kind === 'series' ? 'Series' : 'Films'}</h2>
          {/if}
          <div class="counthead">
            <p class="count" data-testid="recipe-count">{countLine(r)}</p>
            {#if i === 0 && page.for_you_available}
              <div class="segmented sort" role="group" aria-label="Order">
                {#each ORDERS as o (o.id)}
                  <button data-testid="recipe-sort-{o.id}" aria-pressed={page.sort === o.id} onclick={() => (sort = o.id)}
                    >{o.label}</button
                  >
                {/each}
              </div>
            {/if}
          </div>

          {#if r.cells.length}
            <div class="grid">{@render cells(r, split ? r.cells.filter(strong) : r.cells)}</div>
          {/if}
          {#if split && r.cells.some((c) => !strong(c))}
            {#if r.weak}
              <h3 class="list-header sub" data-testid="recipe-weak-head">{page.strong_total ? 'Might also fit' : 'Might fit'}</h3>
              <p class="footnote ours">Our read · less certain</p>
              <div class="grid">{@render cells(r, r.cells.filter((c) => !strong(c)))}</div>
            {:else}
              <div class="more">
                <button class="btn-secondary" data-testid="recipe-weak" onclick={() => (r.weak = true)}>
                  Show {(page.total - page.strong_total).toLocaleString()}{page.strong_total ? ' more' : ''} that might fit
                </button>
              </div>
            {/if}
          {/if}
          {#if rowsIn(r.cells) < page.total && (!split || r.weak || r.cells.every(strong))}
            <div class="more">
              <button class="btn-secondary" onclick={() => more(i)} disabled={r.busy}>
                Show {(page.total - rowsIn(r.cells)).toLocaleString()} more
              </button>
            </div>
          {/if}

          {#if page.beyond_total}
            {#if r.beyond}
              <h3 class="list-header sub" data-testid="recipe-beyond-head">Beyond the library</h3>
              <div class="grid" data-testid="recipe-beyond">{@render cells(r, r.beyond.cells)}</div>
              {#if !r.beyond.busy && rowsIn(r.beyond.cells) < r.beyond.total}
                <div class="more">
                  <button class="btn-secondary" onclick={() => openBeyond(i)}>
                    Show {(r.beyond.total - rowsIn(r.beyond.cells)).toLocaleString()} more
                  </button>
                </div>
              {/if}
            {:else}
              <div class="more">
                <button class="btn-secondary" data-testid="recipe-beyond-open" onclick={() => openBeyond(i)}>
                  Show {page.beyond_total.toLocaleString()} more beyond the library
                </button>
              </div>
            {/if}
          {:else if !page.library_total}
            <div class="empty card" data-testid="recipe-empty">
              <p class="why">Nothing shares enough with this recipe yet. Remove a film or a filter to widen it.</p>
            </div>
          {/if}
        </section>
      {/each}
    </div>
  {/if}
</div>

<style>
  .head {
    display: flex;
    flex-direction: column;
    gap: 2px;
    margin: -4px 0 16px;
  }
  .line {
    margin: 0;
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-3);
  }
  .k {
    color: var(--text-2);
  }
  .regions {
    display: flex;
    flex-direction: column;
    gap: 24px;
    transition: opacity var(--dur-quick) var(--ease);
  }
  .dim {
    opacity: 0.55;
  }
  .kindhead {
    padding: 0;
    margin-bottom: 8px;
  }
  .counthead {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px 12px;
    flex-wrap: wrap;
    min-height: 36px;
    margin-bottom: 12px;
  }
  .count {
    margin: 0;
    font-size: var(--fs-subhead);
    line-height: 20px;
    color: var(--text-2);
  }
  .sort {
    width: 200px;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 20px 12px;
  }
  .ghost {
    display: block;
    aspect-ratio: 2 / 3;
  }
  .foldcell {
    --press: 0.96;
    width: 100%;
    aspect-ratio: 2 / 3;
    display: flex;
    flex-direction: column;
    justify-content: flex-end;
    align-items: flex-start;
    gap: 2px;
    padding: 12px;
    border: none;
    border-radius: var(--r-poster);
    background: var(--surface-2);
    color: var(--text-2);
    text-align: left;
  }
  .plus {
    font-size: var(--fs-title);
    line-height: 1;
    color: var(--text);
  }
  .by {
    font-size: var(--fs-footnote);
    line-height: 18px;
  }
  .sub {
    padding: 0;
    margin: 24px 0 4px;
  }
  .ours {
    margin: 0 0 12px;
  }
  .more {
    display: flex;
    padding: 20px 0 0;
  }
  .empty {
    padding: var(--card-pad-roomy);
    text-align: center;
    display: flex;
    flex-direction: column;
    gap: 8px;
    align-items: center;
  }
  .empty p {
    margin: 0;
    max-width: 46ch;
  }
  @media (min-width: 721px) {
    .grid {
      grid-template-columns: repeat(auto-fill, var(--shelf-poster));
      gap: 24px 16px;
    }
  }
</style>
