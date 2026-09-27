<script>
  // Two modes (§6.0): shelves from `/api/home`, and the catalog grid from `/api/titles`, the only
  // route with every filter. Search or a person filter switches to the grid; clearing returns.
  import { onMount } from 'svelte';
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
  import FinishPrompt from '$lib/components/FinishPrompt.svelte';
  import PendingVerdicts from '$lib/components/PendingVerdicts.svelte';
  import PosterCard, { isColdPlaced } from '$lib/components/PosterCard.svelte';
  import ShelfList from '$lib/components/ShelfList.svelte';
  import TitleDetail from '$lib/components/TitleDetail.svelte';

  // One switch: Films, Series or Both, never neither (decisions 18, 474).
  let kinds = $state(['movie']);
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
  let hidden = $state({});
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
  let home = $state(null);
  let homeLoading = $state(false);
  let homeError = $state('');

  const LIMIT = 60;

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

  const activeFilters = $derived(
    [
      genre ? `genre ${genre}` : null,
      decade ? `${decade}s` : null,
      seen !== 'any' ? seen : null,
      owned ? 'in your library' : null
    ].filter(Boolean)
  );
  // The grid counts the catalog it lists; the shelves count the household's own library.
  const count = $derived(
    mode === 'grid'
      ? countLabel({ total, hidden, kinds, filters: activeFilters })
      : home?.library
        ? libraryLabel({ library: home.library, kinds })
        : ''
  );

  // The shell's own admin test, so Home and the header agree on who reads the operator's words.
  const canAdmin = $derived(
    (session.user?.nav?.account ?? []).some((entry) => entry.key === 'admin')
  );
  // "no bundle imported" is the operator's name for the state (§3.1); a member reads plain words.
  const bundleNote = $derived(
    session.hasBundle
      ? ''
      : session.restartRequired
        ? canAdmin ? ' · bundle imported · restart needed' : ' · waiting for a restart'
        : canAdmin ? ' · no bundle imported' : ' · no movie data yet'
  );

  // The device clock: the household's phones share the install's TZ.
  const greeting = $derived(`${band()}${session.user ? `, ${session.user.name}` : ''}`);

  function band() {
    const h = new Date().getHours();
    if (h < 5) return 'Up late';
    if (h < 12) return 'Good morning';
    if (h < 18) return 'Good afternoon';
    return 'Good evening';
  }

  // Overlapping filter requests: a sequence number keeps a slow earlier answer from landing last.
  let requestSeq = 0;

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
    loading = true;
    loadError = '';
    if (!append) {
      showWeak = false;
      elsewhere = null;
      kindNote = '';
    }
    try {
      const res = await get(titlesQuery(kinds, { offset: append ? offset : 0 }));
      if (seq !== requestSeq) return;      // a newer request has already answered
      items = append ? [...items, ...res.items] : res.items;
      total = res.total;
      hidden = res.hidden ?? {};
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

  // A Show the model flip re-reads the shelves once the server has the preference (the epoch, not
  // the optimistic flag). `lastEpoch` is not `$state`, or the effect would loop.
  let lastEpoch = modelGate.epoch;
  $effect(() => {
    const epoch = modelGate.epoch;
    if (epoch === lastEpoch) return;
    lastEpoch = epoch;
    loadShelves();
  });

  async function chooseKinds(choice) {
    if (kindChoice(kinds) === choice) return;
    kinds = kindsFor(choice);
    // The open card's tier and weight are per-kind, so switching closes it (proposal 32).
    selected = null;
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
      kindNote = `${dropped.join(' and ')} cleared - no ${noun} match ${them}.`;
    }
  }

  let debounce;
  function onQuery() {
    // Close the card on the keystroke, not after the debounce.
    selected = null;
    clearTimeout(debounce);
    debounce = setTimeout(() => load(), 220);
  }

  function filterToPerson(person) {
    // A credit row may fold several person rows of one human (`person_ids`), so filter by the set.
    personIds = person.person_ids?.length ? person.person_ids : [person.person_id ?? person.id];
    personName = person.name;
    selected = null;
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

<!-- Its answer moves the banner's population, so it re-reads the shelves (decision 212). -->
<FinishPrompt onAnswered={loadShelves} />

<div class="head">
  <div class="greetline">
    <h1 data-testid="home-greeting">{greeting}</h1>
  </div>

  <PendingVerdicts banner={home?.banner} />

  <div class="controls">
    <div class="kinds" role="group" aria-label="Kind">
      <!-- On the shelves the switch partitions (§4.1 rule 5); on the grid it is only a filter. -->
      {#each KIND_CHOICES as choice (choice.id)}
        <button
          class="pill"
          data-testid="kind-{choice.id}"
          aria-pressed={kindChoice(kinds) === choice.id}
          onclick={() => chooseKinds(choice.id)}
        >{choice.label}</button>
      {/each}
    </div>

    <div class="searchrow">
      <input
        type="search"
        data-testid="home-search"
        bind:value={q}
        oninput={onQuery}
        placeholder="search title, alias"
        aria-label="Search titles"
      />
      <button
        class="pill filtertoggle"
        aria-expanded={filtersOpen}
        aria-controls="home-filters"
        data-testid="filter-toggle"
        onclick={() => (filtersOpen = !filtersOpen)}
      >{nFilters ? `Filters · ${nFilters}` : 'Filters'}</button>
    </div>
  </div>

  {#if filtersOpen}
    <div class="filterpanel" id="home-filters" data-testid="filter-panel">
      <select
        class="genre"
        bind:value={genre}
        onchange={() => load()}
        aria-label="Genre"
        data-testid="filter-genre"
      >
        <option value="">every genre</option>
        {#each facets.genres as g (g)}<option value={g}>{g}</option>{/each}
      </select>
      <select bind:value={decade} onchange={() => load()} aria-label="Decade" data-testid="filter-decade">
        <option value="">every decade</option>
        {#each facets.decades as d (d)}<option value={d}>{d}s</option>{/each}
      </select>
      <select bind:value={seen} onchange={() => load()} aria-label="Seen state" data-testid="filter-seen">
        <option value="any">seen or not</option>
        <option value="seen">seen</option>
        <option value="unseen">unseen</option>
      </select>
      <button
        class="pill"
        aria-pressed={owned}
        onclick={toggleOwned}
        data-testid="filter-owned"
      >in my library</button>
    </div>
  {/if}

  {#if personIds || (nFilters && !filtersOpen)}
    <div class="filters">
      {#if personIds}
        <!-- The chip is the only way out of a filmography, so it is always visible. -->
        <button class="pill on" onclick={clearPerson} data-testid="person-chip">{personName} ✕</button>
      {/if}
      <!-- A narrowed grid never hides why it is narrow; the open panel already says it. -->
      {#if !filtersOpen}
        {#if genre}
          <button class="pill on" onclick={() => clearFilter('genre')} data-testid="genre-chip">{genre} ✕</button>
        {/if}
        {#if decade}
          <button class="pill on" onclick={() => clearFilter('decade')} data-testid="decade-chip">{decade}s ✕</button>
        {/if}
        {#if seen !== 'any'}
          <button class="pill on" onclick={() => clearFilter('seen')} data-testid="seen-chip">{seen} ✕</button>
        {/if}
        {#if owned}
          <button class="pill on" onclick={() => clearFilter('owned')} data-testid="owned-filter-chip"
            >in my library ✕</button
          >
        {/if}
      {/if}
    </div>
  {/if}

  <div class="data count" data-testid="count-line">
    {count}{bundleNote}
  </div>
  {#if kindNote}
    <p class="why kindnote" role="status" data-testid="kind-filter-note">{kindNote}</p>
  {/if}
</div>

{#if home?.degraded && home.degraded.state !== 'no_bundle'}
  <!-- `no_bundle` is rendered further down, by the panel the first-boot spec asserts. -->
  <div class="card degraded" data-testid="home-degraded" data-state={home.degraded.state}>
    <h2>{home.degraded.headline}</h2>
    <p class="why">{home.degraded.why}</p>
    {#if home.degraded.cta}
      <a class="btn-primary" href={home.degraded.cta.route}>{home.degraded.cta.label}</a>
    {/if}
  </div>
{/if}

<!-- Only an admin gets §3.1's name for the state and a door; a member gets plain words and none. -->
{#snippet noBundle()}
  <h2>Nothing to show yet</h2>
  {#if session.restartRequired}
    {#if canAdmin}
      <p class="why">
        A bundle is imported and this server has not loaded it yet. The Data tab says why.
      </p>
      <a class="btn-primary" href="/admin/data">Open the Data tab</a>
    {:else}
      <p class="why">
        The movie data is waiting for a restart. Your shelves appear here once it has loaded.
      </p>
    {/if}
  {:else if canAdmin}
    <p class="why">
      No artifact bundle has been imported. That is a legal state — the app runs, the setup
      wizard and admin routes work, and every artifact-dependent surface says so instead of
      erroring.
    </p>
    <a class="btn-primary" href="/admin/data">Import a bundle</a>
  {:else}
    <p class="why">There is no movie data yet. Once an admin adds it, your shelves appear here.</p>
  {/if}
{/snippet}

{#if mode === 'grid'}
  <div class="gridhead">
    <p class="modeline why" data-testid="home-mode" data-mode="grid" data-reason={reason}>
      {gridLine(reason)}
    </p>
    {#if sortOffered(reason, sortEcho, forYouAvailable)}
      <!-- The pressed position is the order the server says it used, never the one asked for. -->
      <div class="sort" role="group" aria-label="Order">
        {#each SORT_CHOICES as c (c.id)}
          <button
            class="pill"
            data-testid="sort-{c.id}"
            aria-pressed={sortEcho === c.id}
            onclick={() => chooseSort(c.id)}
          >{c.label}</button>
        {/each}
      </div>
    {:else if sortWaitingLine(reason, sortEcho, forYouAvailable)}
      <p class="why sortwait" data-testid="sort-waiting">
        {sortWaitingLine(reason, sortEcho, forYouAvailable)}
      </p>
    {/if}
    {#if partitioned}
      <p class="why partition" data-testid="grid-partition">{partitionLine(kinds, sortEcho)}</p>
    {/if}
  </div>

  {#if loadError}
    <div class="empty card"><p>{loadError}</p></div>
  {:else if !items.length && !loading}
    <div class="empty card">
      {#if !session.hasBundle}
        {@render noBundle()}
      {:else if elsewhere}
        <h2>Not in {kindChoice(kinds) === 'series' ? 'series' : 'films'}</h2>
        <p class="why" data-testid="found-elsewhere">
          {elsewhereLine(elsewhere.kind, elsewhere.names, elsewhere.total)}
        </p>
        <button
          class="btn-primary"
          data-testid="found-elsewhere-switch"
          onclick={() => chooseKinds(elsewhere.kind)}
        >{elsewhere.kind === 'series' ? 'Show series' : 'Show films'}</button>
      {:else}
        <h2>No matches</h2>
        <!-- Names the dimensions the catalog search really has. -->
        <!-- The grid lists the whole catalog unless "in my library" is on. -->
        <p class="why">{owned ? 'Nothing in your library matches.' : 'Nothing matches.'}</p>
        <p class="data" data-testid="no-matches-help">
          search reads the title and its aliases · the kind switch and Filters (genre, decade,
          seen state, "in my library") narrow it further · clear a chip to widen it
        </p>
      {/if}
    </div>
  {:else}
    {#if items.some(isColdPlaced)}
      <!-- The badge's why, said once for the grid: a title= tooltip does not exist on touch. -->
      <p class="why" data-testid="catalog-cold-note">
        Cards marked "new" have no outside ratings yet — we placed them by what they're about.
      </p>
    {/if}
    <div class="grid">
      {#each strongItems as t, i (t.id)}
        {#if partitioned && kindHeading(strongItems, i)}
          <h2 class="kindhead" data-testid="grid-kind-{t.kind}">{kindHeading(strongItems, i)}</h2>
        {/if}
        <PosterCard title={t} onSelect={() => (selected = t.id)} />
      {/each}
    </div>
    {#if weakItems.length}
      <!-- Hits that only contain the letters wait behind one button. -->
      {#if showWeak}
        <p class="why weakhead" data-testid="weak-matches-head">Looser matches</p>
        <div class="grid" data-testid="weak-matches">
          {#each weakItems as t (t.id)}
            <PosterCard title={t} onSelect={() => (selected = t.id)} />
          {/each}
        </div>
      {:else}
        <div class="more">
          <button class="btn-ghost" data-testid="weak-matches-toggle" onclick={() => (showWeak = true)}>
            {`Show ${weakTotal.toLocaleString()} looser ${weakTotal === 1 ? 'match' : 'matches'}`}
          </button>
        </div>
      {/if}
    {/if}
    {#if offset < total && (showWeak || !weakItems.length)}
      <div class="more">
        <button class="btn-ghost" onclick={() => load({ append: true })} disabled={loading}>
          {loading ? 'Loading…' : `Show more · ${(total - offset).toLocaleString()} left`}
        </button>
      </div>
    {/if}
  {/if}
{:else if !session.hasBundle}
  <div class="empty card">
    {@render noBundle()}
  </div>
{:else}
  <!-- The marker stays for the tests that read `data-mode`. -->
  <div class="sr-only" data-testid="home-mode" data-mode="shelves">your shelves</div>
  {#if homeError}
    <div class="empty card"><p>{homeError}</p></div>
  {:else}
    <ShelfList payload={home} loading={homeLoading} onSelect={(id) => (selected = id)} />
  {/if}
{/if}

{#if selected}
  <TitleDetail
    titleId={selected}
    onClose={() => (selected = null)}
    onPerson={filterToPerson}
    onStateChange={onSeenChange}
  />
{/if}

<style>
  .head {
    display: flex;
    flex-direction: column;
    gap: 12px;
    margin-bottom: 18px;
  }
  .greetline {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  h1 {
    margin: 0;
    font-size: 21px;
    font-weight: 600;
  }
  .controls {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }
  .kinds {
    display: flex;
    gap: 6px;
  }
  .searchrow {
    display: flex;
    gap: 8px;
    flex: 1 1 260px;
    min-width: 0;
    max-width: 520px;
  }
  .searchrow input {
    flex: 1;
    min-width: 0;
  }
  .filtertoggle {
    flex: none;
  }
  .filterpanel {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    padding: 10px;
    border: 1px solid var(--line);
    border-radius: var(--r-md);
    background: var(--card);
  }
  .filters {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  .kindnote {
    margin: -6px 0 0;
  }
  select {
    padding: 7px 10px;
    border-radius: var(--r-pill);
    border: 1px solid var(--line-2);
    background: var(--card);
    font-family: var(--mono);
    font-size: 11px;
    color: var(--ink-3);
  }
  /* 16px: iOS Safari zooms on focus below it, and this scoped rule outranks design.css's 16px. */
  @media (pointer: coarse) {
    select {
      font-size: 16px;
    }
  }
  /* A native select is as wide as its longest option, which can outgrow a phone. */
  .filterpanel select {
    max-width: 100%;
    min-width: 0;
    text-overflow: ellipsis;
  }
  .filterpanel select.genre {
    flex: 1 1 12rem;
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
  .count {
    letter-spacing: 0.04em;
  }
  .gridhead {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px 12px;
    flex-wrap: wrap;
    margin-bottom: 14px;
  }
  .modeline {
    margin: 0;
  }
  .sort {
    display: flex;
    gap: 6px;
  }
  .sortwait,
  .partition {
    margin: 0;
    flex-basis: 100%;
  }
  .kindhead {
    grid-column: 1 / -1;
    margin: 8px 0 0;
    font-size: 15px;
  }
  .weakhead {
    margin: 22px 0 10px;
  }
  .degraded {
    margin-bottom: 18px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    align-items: flex-start;
    border-color: var(--ember-edge);
    background: var(--ember-wash);
  }
  .degraded h2 {
    margin: 0;
    font-size: 16px;
    font-weight: 600;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(132px, 1fr));
    gap: 14px;
  }
  .more {
    display: flex;
    justify-content: center;
    padding: 22px 0;
  }
  .empty {
    padding: var(--card-pad-roomy);
    text-align: center;
    display: flex;
    flex-direction: column;
    gap: 10px;
    align-items: center;
  }
  .empty h2 {
    margin: 0;
    font-size: 17px;
    font-weight: 600;
  }
  .empty .why {
    max-width: 46ch;
  }
  @media (max-width: 720px) {
    .grid {
      grid-template-columns: repeat(auto-fill, minmax(104px, 1fr));
      gap: 10px;
    }
  }
</style>
