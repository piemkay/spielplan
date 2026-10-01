<script>
  // Two modes (§6.0): shelves from `/api/home`, and the catalog grid from `/api/titles`, the only
  // route with every filter. Search or a person filter switches to the grid; clearing returns.
  import { onMount } from 'svelte';
  import { afterNavigate, beforeNavigate } from '$app/navigation';
  import { get, qs } from '$lib/api.js';
  import { session } from '$lib/session.svelte.js';
  import {
    KIND_CHOICES,
    SORT_CHOICES,
    activeFilterCount,
    countLabel,
    elsewhereLine,
    gridLine,
    gridReason,
    homeKept,
    homeMode,
    kindChoice,
    kindHeading,
    kindsFor,
    libraryLabel,
    loadHome,
    modelGate,
    otherKinds,
    partitionLine,
    partitionedByKind,
    sortOffered,
    sortWaitingLine,
    strongEnd
  } from '$lib/home.svelte.js';
  import { publishSuppressed } from '$lib/rail.svelte.js';
  import { displayNames } from '$lib/titleCard.js';
  import { topbar } from '$lib/topbar.svelte.js';
  import { wishes } from '$lib/wish.svelte.js';
  import ArrivedBanner from '$lib/components/ArrivedBanner.svelte';
  import FinishPrompt from '$lib/components/FinishPrompt.svelte';
  import PendingVerdicts from '$lib/components/PendingVerdicts.svelte';
  import PosterCard, { isColdPlaced } from '$lib/components/PosterCard.svelte';
  import ShelfList from '$lib/components/ShelfList.svelte';
  import TitleDetail from '$lib/components/TitleDetail.svelte';

  // Back from another tab, the last shelves show at once and are re-read quietly (decision 530).
  const kept = homeKept.user === session.user?.id && homeKept.epoch === modelGate.epoch ? homeKept : null;

  // One switch: Films, Series or Both, never neither (decisions 18, 474).
  let kinds = $state(kept?.kinds ?? ['movie']);
  let q = $state('');
  let genre = $state('');
  let decade = $state('');
  let seen = $state('any');
  let owned = $state(false);
  // The person filter is a SET of person ids: see `filterToPerson`.
  let personIds = $state(null);
  let personName = $state('');

  let items = $state([]);
  let total = $state(0);
  let offset = $state(0);
  let loading = $state(false);
  let facets = $state({ genres: [], decades: [] });
  let selected = $state(null);
  let loadError = $state('');
  let filtersOpen = $state(false);
  // `sortEcho` is the order the server says it used, which the control shows pressed.
  let sort = $state(null);
  let sortEcho = $state(null);
  // The server's `for_you_available`; null from a build that does not say.
  let forYouAvailable = $state(null);
  // An empty grid whose other kind holds matches: `{kind, names, total}`.
  let elsewhere = $state(null);
  let showWeak = $state(false);
  // Describes one kind switch only, so the next list the page asks for clears it.
  let kindNote = $state('');

  /** @type {any} */
  let home = $state(kept?.payload ?? null);
  let homeLoading = $state(false);
  let homeError = $state('');
  const shelvesStale = $derived(homeLoading && kindChoice(home?.kinds ?? []) !== kindChoice(kinds));

  const LIMIT = 60;
  const SEEN_WORDS = { any: 'Seen or not', seen: 'Seen', unseen: 'Not seen' };

  const mode = $derived(homeMode({ q, personId: personIds, genre, decade, seen, owned }));
  const reason = $derived(gridReason({ q, personId: personIds, genre, decade, seen, owned }));
  const nFilters = $derived(activeFilterCount({ genre, decade, seen, owned }));
  // The weak tail of a search is folded, not dropped.
  const cut = $derived(reason === 'search' ? strongEnd(items, q) : 0);
  const weakItems = $derived(cut ? items.slice(cut) : []);
  const strongItems = $derived(weakItems.length ? items.slice(0, cut) : items);
  // Everything after the last strong hit is weaker, loaded or not: the list is ordered by quality.
  const weakTotal = $derived(weakItems.length ? total - cut : 0);
  const partitioned = $derived(partitionedByKind(kinds, sortEcho));

  // The shell renders Home's suppressed list; clear it on teardown, or it outlives this surface.
  $effect(() => {
    publishSuppressed(home?.suppressed);
    return () => publishSuppressed([]);
  });

  // The grid counts the catalog it lists; the shelves count the household's own library.
  const count = $derived(
    mode === 'grid'
      ? countLabel({ total, kinds, owned })
      : home?.library
        ? libraryLabel({ library: home.library, kinds })
        : ''
  );

  // The shell's own admin test, so Home and the header agree on who gets the door to Movie data.
  const canAdmin = $derived(
    (session.user?.nav?.account ?? []).some((entry) => entry.key === 'admin')
  );
  const bundleNote = $derived(
    session.hasBundle ? '' : session.restartRequired ? 'waiting for a restart' : 'no movie data yet'
  );
  // The count is the search field's placeholder (decision 528). With no movie data a count of
  // nothing says nothing: the note stands alone, under the field.
  const placeholder = $derived(count && !bundleNote ? `Search ${count}` : 'Search');
  const note = $derived(bundleNote.replace(/^./, (c) => c.toUpperCase()));

  // The kind switch is the shell's top row; on a wide screen the search joins it (decision 528).
  let width = $state(0);
  const wide = $derived(width >= 1100);
  $effect(() => {
    if (!topbar.host) return;
    topbar.content = homeBar;
    return () => {
      if (topbar.content === homeBar) topbar.content = null;
    };
  });

  // Overlapping filter requests: a sequence number keeps a slow earlier answer from landing last.
  let requestSeq = 0;

  // The question the listed grid answers. From the keystroke on, a list for another one is dimmed,
  // and one for another reason (the catalog under a first search letter) is not shown at all.
  let shown = $state({ query: null, reason: null });
  const gridStale = $derived(shown.query !== titlesQuery(kinds));

  function titlesQuery(forKinds, { limit = LIMIT, offset: from = 0 } = {}) {
    return `/titles${qs({
      kind: forKinds,
      q,
      genre,
      decade: decade || undefined,
      seen: seen === 'any' ? undefined : seen,
      person_id: personIds ?? undefined,
      owned_only: owned || undefined,
      // A search is best match first (decision 472) whatever order a filtered grid was put in.
      sort: q.trim() ? undefined : (sort ?? undefined),
      limit,
      offset: from
    })}`;
  }

  async function load({ append = false } = {}) {
    const seq = ++requestSeq;
    const query = titlesQuery(kinds, { offset: append ? offset : 0 });
    const asked = reason;
    loading = true;
    loadError = '';
    if (!append) {
      showWeak = false;
      elsewhere = null;
      kindNote = '';
    }
    try {
      const res = await get(query);
      if (seq !== requestSeq) return;      // a newer request has already answered
      if (!append) shown = { query, reason: asked };
      items = append ? [...items, ...res.items] : res.items;
      total = res.total;
      offset = (append ? offset : 0) + res.items.length;
      // Absent from a build that does not echo it, and then no order control is offered.
      sortEcho = res.sort ?? null;
      forYouAvailable = res.for_you_available ?? null;
      if (!append && !res.items.length) findElsewhere(seq, res.hidden ?? {});
    } catch (err) {
      if (seq === requestSeq) loadError = err.message;
    } finally {
      if (seq === requestSeq) loading = false;
    }
  }

  // An empty grid names matches in the kind it was not showing, tied to the grid's own seq.
  async function findElsewhere(seq, counts) {
    const kind = otherKinds(kinds).find((k) => (counts[k] ?? 0) > 0);
    if (!kind) return;
    const res = await get(titlesQuery([kind], { limit: 2 })).catch(() => null);
    if (seq !== requestSeq || !res?.items?.length) return;
    elsewhere = { kind, names: res.items.map((t) => displayNames(t).primary), total: res.total };
  }

  function chooseSort(id) {
    if (sortEcho === id) return;
    sort = id;
    load();
  }

  let homeSeq = 0;

  // Depends on the kind selection only, so search does not refetch it.
  async function loadShelves() {
    const seq = ++homeSeq;
    homeLoading = true;
    homeError = '';
    try {
      const res = await loadHome(kinds);
      if (seq !== homeSeq) return;
      home = res;
      Object.assign(homeKept, { user: session.user?.id, epoch: modelGate.epoch, kinds: [...kinds], payload: res });
    } catch (err) {
      if (seq === homeSeq) homeError = err.message;
    } finally {
      if (seq === homeSeq) homeLoading = false;
    }
  }

  // Its own counter: `load()` bumps `requestSeq` alone, which must not discard the newest facets read.
  let facetSeq = 0;

  /** The facets it applied; null when the read failed, undefined when a newer read superseded it. */
  async function loadFacets() {
    const seq = ++facetSeq;
    const found = await get(`/facets${qs({ kind: kinds })}`).catch(() => null);
    if (seq !== facetSeq) return undefined;   // a newer request has already answered
    facets = found ?? { genres: [], decades: [] };
    return found;
  }

  onMount(async () => {
    await Promise.all([load(), loadFacets(), loadShelves()]);
  });

  // A tab's link lands at the top, so Home puts back its kept place; Back restores its own.
  let restoreY = kept?.scrollY ?? 0;
  beforeNavigate(() => {
    homeKept.scrollY = mode === 'shelves' ? window.scrollY : 0;
  });
  afterNavigate(({ type }) => {
    if (restoreY && type !== 'popstate') window.scrollTo(0, restoreY);
    restoreY = 0;
  });

  // A Show the model flip re-reads the shelves once the server has the preference (the epoch, not
  // the optimistic flag). `lastEpoch` is not `$state`, or the effect would loop.
  let lastEpoch = modelGate.epoch;
  $effect(() => {
    const epoch = modelGate.epoch;
    if (epoch === lastEpoch) return;
    lastEpoch = epoch;
    loadShelves();
  });

  // A wish written anywhere (a sheet, the banner, a title card) moves Home's banner, row and shelf.
  let lastWishes = wishes.epoch;
  $effect(() => {
    const epoch = wishes.epoch;
    if (epoch === lastWishes) return;
    lastWishes = epoch;
    loadShelves();
  });

  async function chooseKinds(choice) {
    if (kindChoice(kinds) === choice) return;
    kinds = kindsFor(choice);
    kindNote = '';
    loadShelves();
    // A filter the new kinds carry stays; one they lack is cleared and named.
    const found = await loadFacets();
    if (found === undefined) return;     // a newer tap is on its way and will load
    const dropped = [];
    // A failed read says nothing about the new kind's vocabulary, so it clears nothing.
    if (found && genre && !found.genres.includes(genre)) {
      dropped.push(genre);
      genre = '';
    }
    if (found && decade && !found.decades.map(String).includes(String(decade))) {
      dropped.push(`${decade}s`);
      decade = '';
    }
    // Load first: a new list clears the old note, then this switch names what it cleared.
    load();
    if (dropped.length) {
      const noun = choice === 'series' ? 'series' : choice === 'movie' ? 'films' : 'titles';
      const them = dropped.length > 1 ? 'them' : 'it';
      kindNote = `${dropped.join(' and ')} cleared — no ${noun} match ${them}.`;
    }
  }

  let debounce;
  function onQuery() {
    clearTimeout(debounce);
    debounce = setTimeout(() => load(), 220);
  }

  // The title card has closed itself by now, so its history entry is gone (decision 527).
  function filterToPerson(person) {
    // A credit row may fold several person rows of one human (`person_ids`), so filter by the set.
    personIds = person.person_ids?.length ? person.person_ids : [person.person_id ?? person.id];
    personName = person.name;
    q = '';
    load();
  }

  // With the seen filter active, a toggled card stops matching and leaves.
  function onSeenChange(titleId, state) {
    items = items.map((t) => (t.id === titleId ? { ...t, seen_state: state } : t));
    if (seen !== 'any' && seen !== state) load();
    // The banner is the server's population, so re-read it.
    loadShelves();
  }

  function clearPerson() {
    personIds = null;
    personName = '';
    load();
  }

  function clearFilter(which) {
    if (which === 'genre') genre = '';
    if (which === 'decade') decade = '';
    if (which === 'seen') seen = 'any';
    if (which === 'owned') owned = false;
    load();
  }

  function toggleOwned() {
    owned = !owned;
    load();
  }
</script>

{#snippet icon(name)}
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    {#if name === 'search'}
      <circle cx="11" cy="11" r="6.5" /><path d="m16 16 4.5 4.5" />
    {:else if name === 'filter'}
      <path d="M4 7h10M18 7h2M4 17h4M12 17h8" /><circle cx="16" cy="7" r="2" /><circle cx="10" cy="17" r="2" />
    {:else if name === 'close'}
      <path d="M6 6l12 12M18 6 6 18" />
    {:else if name === 'chevron'}
      <path d="m5.5 9.5 6.5 6.5 6.5-6.5" />
    {/if}
  </svg>
{/snippet}

<!-- A removable chip: the whole chip clears what it names. -->
{#snippet chip(label, testid, onclick)}
  <button class="pill on" data-testid={testid} aria-label="Remove {label}" {onclick}>
    {label}{@render icon('close')}
  </button>
{/snippet}

{#snippet searchRow()}
  <div class="searchrow">
    <label class="search">
      {@render icon('search')}
      <input
        type="search"
        data-testid="home-search"
        bind:value={q}
        oninput={onQuery}
        {placeholder}
        aria-label="Search titles"
      />
    </label>
    <button
      class="pill"
      aria-expanded={filtersOpen}
      aria-controls={filtersOpen ? 'home-filters' : undefined}
      data-testid="filter-toggle"
      onclick={() => (filtersOpen = !filtersOpen)}
    >{@render icon('filter')}{nFilters ? `Filters · ${nFilters}` : 'Filters'}</button>
  </div>
{/snippet}

{#snippet homeBar()}
  <div class="bar">
    <!-- On the shelves the switch partitions (§4.1 rule 5); on the grid it is only a filter. -->
    <div class="segmented kinds" role="group" aria-label="Kind">
      {#each KIND_CHOICES as choice (choice.id)}
        <button
          data-testid="kind-{choice.id}"
          aria-pressed={kindChoice(kinds) === choice.id}
          onclick={() => chooseKinds(choice.id)}
        >{choice.label}</button>
      {/each}
    </div>
    {#if wide}{@render searchRow()}{/if}
  </div>
{/snippet}

<svelte:window bind:innerWidth={width} />

<h1 class="sr-only" data-testid="home-title">Home</h1>
{#if !topbar.host}{@render homeBar()}{/if}

<div class="controls">
  {#if !wide}{@render searchRow()}{/if}
  {#if note}<p class="footnote count" data-testid="count-line">{note}</p>{/if}
</div>

{#if filtersOpen}
  <div class="list-group filterpanel" id="home-filters" data-testid="filter-panel">
    <!-- The select covers its row, so a tap anywhere on the row opens the native picker. -->
    <div class="list-row field">
      <span>Genre</span>
      <span class="value">{genre || 'Any'}{@render icon('chevron')}</span>
      <select bind:value={genre} onchange={() => load()} aria-label="Genre" data-testid="filter-genre">
        <option value="">Any genre</option>
        {#each facets.genres as g (g)}<option value={g}>{g}</option>{/each}
      </select>
    </div>
    <div class="list-row field">
      <span>Decade</span>
      <span class="value">{decade ? `${decade}s` : 'Any'}{@render icon('chevron')}</span>
      <select bind:value={decade} onchange={() => load()} aria-label="Decade" data-testid="filter-decade">
        <option value="">Any decade</option>
        {#each facets.decades as d (d)}<option value={d}>{d}s</option>{/each}
      </select>
    </div>
    <div class="list-row field">
      <span>Seen</span>
      <span class="value">{SEEN_WORDS[seen]}{@render icon('chevron')}</span>
      <select bind:value={seen} onchange={() => load()} aria-label="Seen state" data-testid="filter-seen">
        <option value="any">Seen or not</option>
        <option value="seen">Seen</option>
        <option value="unseen">Not seen</option>
      </select>
    </div>
    <div class="list-row">
      <span id="owned-label">In my library</span>
      <button
        class="switch"
        role="switch"
        aria-checked={owned}
        aria-labelledby="owned-label"
        onclick={toggleOwned}
        data-testid="filter-owned"
      ><span class="knob"></span></button>
    </div>
  </div>
{/if}

{#if personIds || (nFilters && !filtersOpen)}
  <div class="chips">
    {#if personIds}
      <!-- The chip is the only way out of a filmography, so it is always visible. -->
      {@render chip(personName, 'person-chip', clearPerson)}
    {/if}
    <!-- A narrowed grid never hides why it is narrow; the open panel already says it. -->
    {#if !filtersOpen}
      {#if genre}{@render chip(genre, 'genre-chip', () => clearFilter('genre'))}{/if}
      {#if decade}{@render chip(`${decade}s`, 'decade-chip', () => clearFilter('decade'))}{/if}
      {#if seen !== 'any'}{@render chip(SEEN_WORDS[seen], 'seen-chip', () => clearFilter('seen'))}{/if}
      {#if owned}{@render chip('In my library', 'owned-filter-chip', () => clearFilter('owned'))}{/if}
    {/if}
  </div>
{/if}

{#if kindNote}
  <p class="footnote kindnote" role="status" data-testid="kind-filter-note">{kindNote}</p>
{/if}

<!-- Its answer moves the banner's population, so it re-reads the shelves (decision 212). -->
<FinishPrompt onAnswered={loadShelves} />
<PendingVerdicts banner={home?.banner} />
<ArrivedBanner arrived={home?.arrived ?? []} onSelect={(title) => (selected = title)} />

{#if home?.setup_notice}
  {@const notice = home.setup_notice}
  <div class="card notice" data-testid="home-setup-notice">
    <h2 class="section-title">{notice.headline}</h2>
    <p class="why">{notice.why}</p>
    <a class="btn-primary" href={notice.cta.route}>{notice.cta.label}</a>
  </div>
{/if}

<!-- One message for both roles; an admin also gets the door to Movie data. -->
{#snippet noBundle()}
  <h2 class="section-title">Nothing to show yet</h2>
  {#if canAdmin}
    <p class="why">
      {session.restartRequired
        ? 'New movie data is waiting for a restart — Movie data says what to do.'
        : 'No movie data yet. Import it in Movie data and your shelves appear here.'}
    </p>
    <a class="btn-primary" href="/admin/movie-data">Open Movie data</a>
  {:else if session.restartRequired}
    <p class="why">
      The movie data is waiting for a restart. Your shelves appear here once it has loaded.
    </p>
  {:else}
    <p class="why">There is no movie data yet. Once an admin adds it, your shelves appear here.</p>
  {/if}
{/snippet}

{#if mode === 'grid'}
  <div class="gridhead" data-testid="home-mode" data-mode="grid" data-reason={reason}>
    {#if gridLine(reason)}
      <p class="footnote">{gridLine(reason)}</p>
    {/if}
    {#if sortOffered(reason, sortEcho, forYouAvailable)}
      <!-- The pressed position is the order the server says it used, never the one asked for. -->
      <div class="segmented sort" role="group" aria-label="Order">
        {#each SORT_CHOICES as c (c.id)}
          <button
            data-testid="sort-{c.id}"
            aria-pressed={sortEcho === c.id}
            onclick={() => chooseSort(c.id)}
          >{c.label}</button>
        {/each}
      </div>
    {:else if sortWaitingLine(reason, sortEcho, forYouAvailable)}
      <p class="footnote" data-testid="sort-waiting">
        {sortWaitingLine(reason, sortEcho, forYouAvailable)}
      </p>
    {/if}
    {#if partitioned}
      <p class="footnote" data-testid="grid-partition">{partitionLine(kinds, sortEcho)}</p>
    {/if}
  </div>

  {#if loadError}
    <div class="empty card"><p class="why">{loadError}</p></div>
  {:else if gridStale && shown.reason !== reason}
    <div class="grid" aria-hidden="true">
      {#each { length: 9 }, i (i)}<span class="skeleton cell"></span>{/each}
    </div>
  {:else if !items.length && !loading}
    <div class="empty card">
      {#if !session.hasBundle}
        {@render noBundle()}
      {:else if elsewhere}
        <h2 class="section-title">Not in {kindChoice(kinds) === 'series' ? 'series' : 'films'}</h2>
        <p class="why" data-testid="found-elsewhere">
          {elsewhereLine(elsewhere.kind, elsewhere.names, elsewhere.total)}
        </p>
        <button
          class="btn-secondary"
          data-testid="found-elsewhere-switch"
          onclick={() => chooseKinds(elsewhere.kind)}
        >{elsewhere.kind === 'series' ? 'Show series' : 'Show films'}</button>
      {:else}
        <h2 class="section-title">No matches</h2>
        <!-- The grid lists the whole catalog unless "in my library" is on. -->
        <p class="why">{owned ? 'Nothing in your library matches.' : 'Nothing matches.'}</p>
        <!-- Names the dimensions the catalog search really has. -->
        <p class="footnote" data-testid="no-matches-help">
          Search reads titles and their aliases. The kind switch and Filters (genre, decade, seen
          state, in my library) narrow it; clear a chip to widen it.
        </p>
      {/if}
    </div>
  {:else}
    <div class="dims" aria-busy={gridStale}>
      {#if items.some(isColdPlaced)}
        <!-- The badge's why, said once for the grid: a title= tooltip does not exist on touch. -->
        <p class="footnote" data-testid="catalog-cold-note">
          Titles marked New have no outside ratings yet — we placed them by what they're about.
        </p>
      {/if}
      <div class="grid">
        {#each strongItems as t, i (t.id)}
          {#if partitioned && kindHeading(strongItems, i)}
            <h2 class="list-header kindhead" data-testid="grid-kind-{t.kind}">{kindHeading(strongItems, i)}</h2>
          {/if}
          <PosterCard title={t} onSelect={() => (selected = t)} />
        {/each}
      </div>
      {#if weakItems.length}
        <!-- Hits that only contain the letters wait behind one button. -->
        {#if showWeak}
          <h2 class="list-header weakhead" data-testid="weak-matches-head">Looser matches</h2>
          <div class="grid" data-testid="weak-matches">
            {#each weakItems as t (t.id)}
              <PosterCard title={t} onSelect={() => (selected = t)} />
            {/each}
          </div>
        {:else}
          <div class="more">
            <button class="btn-secondary" data-testid="weak-matches-toggle" onclick={() => (showWeak = true)}>
              {`Show ${weakTotal.toLocaleString()} looser ${weakTotal === 1 ? 'match' : 'matches'}`}
            </button>
          </div>
        {/if}
      {/if}
      {#if offset < total && (showWeak || !weakItems.length)}
        <div class="more">
          <button class="btn-secondary" onclick={() => load({ append: true })} disabled={loading}>
            {loading ? 'Loading…' : `Show ${(total - offset).toLocaleString()} more`}
          </button>
        </div>
      {/if}
    </div>
  {/if}
{:else if !session.hasBundle}
  <div class="empty card">
    {@render noBundle()}
  </div>
{:else}
  <!-- The marker stays for the tests that read `data-mode`. -->
  <div class="sr-only" data-testid="home-mode" data-mode="shelves">Your shelves</div>
  {#if homeError}
    <div class="empty card"><p class="why">{homeError}</p></div>
  {:else}
    <ShelfList
      payload={home}
      loading={homeLoading}
      stale={shelvesStale}
      onSelect={(title) => (selected = title)}
    />
  {/if}
{/if}

{#if selected}
  <TitleDetail
    titleId={selected.id}
    seed={selected}
    onClose={() => (selected = null)}
    onPerson={filterToPerson}
    onStateChange={onSeenChange}
  />
{/if}

<style>
  .bar {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .kinds {
    flex: none;
    width: 208px;
    min-height: 32px;
  }
  .kinds > button {
    font-size: var(--fs-footnote);
    line-height: 18px;
  }
  /* The compact switch keeps a 48px tap; so does the row that holds it. */
  @media (pointer: coarse) {
    .bar {
      min-height: 48px;
    }
    .kinds > button::after {
      inset: -12px -2px;
    }
  }
  .controls {
    display: flex;
    flex-direction: column;
    gap: 12px;
    margin: 8px 0 16px;
  }
  .searchrow {
    display: flex;
    gap: 8px;
    align-items: center;
  }
  .search {
    flex: 1;
    min-width: 0;
    display: flex;
    align-items: center;
    gap: 8px;
    padding-left: 12px;
    border-radius: var(--r-sm);
    background: var(--surface-2);
    color: var(--text-3);
  }
  /* Outranks design.css's field rule: the label draws the field, the input only holds the text. */
  .search > input[type='search'][aria-label] {
    flex: 1;
    min-width: 0;
    padding: 0 12px 0 0;
    background: none;
    border-radius: 0;
    outline: none;
  }
  .search:focus-within {
    outline: 2px solid var(--accent-text);
    outline-offset: 2px;
  }
  .count {
    margin: -4px 4px 0;
  }

  .filterpanel {
    margin-bottom: 16px;
  }
  .field {
    position: relative;
  }
  .field:focus-within {
    outline: 2px solid var(--accent-text);
    outline-offset: -2px;
  }
  .field .value {
    margin-left: auto;
    display: inline-flex;
    align-items: center;
    gap: 4px;
    color: var(--text-3);
  }
  /* Covers its row; the row's own text says what is chosen. */
  .field select {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    min-height: 0;
    opacity: 0;
    cursor: pointer;
  }

  .chips {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    margin-bottom: 16px;
  }
  .kindnote {
    margin: 0 4px 16px;
  }
  .notice {
    display: flex;
    flex-direction: column;
    gap: 8px;
    align-items: flex-start;
    margin-bottom: 32px;
  }
  .notice .why {
    margin: 0 0 4px;
  }
  .notice .btn-primary {
    min-height: var(--touch);
  }

  /* In flow, not absolute: `main` is not a containing block, so an absolute marker scrolled the page. */
  .sr-only {
    width: 1px;
    height: 1px;
    margin: -1px;
    padding: 0;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
    border: 0;
  }

  .gridhead {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px 12px;
    flex-wrap: wrap;
    margin-bottom: 12px;
  }
  .gridhead p {
    margin: 0;
  }
  .sort {
    width: 200px;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 20px 12px;
  }
  .cell {
    display: block;
    aspect-ratio: 2 / 3;
  }
  .kindhead,
  .weakhead {
    padding: 0;
  }
  .kindhead {
    grid-column: 1 / -1;
  }
  .weakhead {
    margin: 32px 0 12px;
  }
  .more {
    display: flex;
    justify-content: center;
    padding: 24px 0;
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
  .empty .btn-primary,
  .empty .btn-secondary {
    margin-top: 8px;
  }

  @media (min-width: 721px) {
    .kinds {
      width: 240px;
    }
    .searchrow {
      width: min(100%, 460px);
    }
    .filterpanel {
      max-width: 460px;
    }
    .notice {
      max-width: 560px;
    }
    .notice .why {
      text-wrap: pretty;
    }
    .grid {
      grid-template-columns: repeat(auto-fill, minmax(148px, 1fr));
      gap: 24px 16px;
    }
  }
</style>
