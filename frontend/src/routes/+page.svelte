<script>
  // Two modes (§6.0): shelves from `/api/home`, and the catalog grid from `/api/titles`, the only
  // route with every filter. Any filter away from its default switches to the grid; clearing returns.
  import { onMount, untrack } from 'svelte';
  import { afterNavigate, beforeNavigate, replaceState } from '$app/navigation';
  import { page } from '$app/state';
  import { get, qs } from '$lib/api.js';
  import { jumpedFrom, unwind } from '$lib/cardJump.js';
  import { session } from '$lib/session.svelte.js';
  import {
    KIND_CHOICES,
    SORT_CHOICES,
    activeFilterCount,
    countLabel,
    dropLabel,
    elsewhereLine,
    emptyLine,
    gridLine,
    gridReason,
    homeKept,
    homeMode,
    kindChoice,
    kindHeading,
    kindsFor,
    loadHome,
    modelGate,
    otherKinds,
    partitionLine,
    partitionedByKind,
    searchPlaceholder,
    sortOffered,
    sortWaitingLine,
    strongEnd,
    withoutChip
  } from '$lib/home.svelte.js';
  import {
    catalogParams,
    chipOrder,
    homeFilters,
    readHomeUrl,
    resetHomeFilters,
    writeHomeUrl
  } from '$lib/homeFilters.svelte.js';
  import { putAway } from '$lib/notices.js';
  import { publishSuppressed } from '$lib/rail.svelte.js';
  import { displayNames } from '$lib/titleCard.js';
  import { showToast } from '$lib/toast.svelte.js';
  import { topbar } from '$lib/topbar.svelte.js';
  import { wishes } from '$lib/wish.svelte.js';
  import ArrivedBanner from '$lib/components/ArrivedBanner.svelte';
  import FilterChip from '$lib/components/FilterChip.svelte';
  import FinishPrompt from '$lib/components/FinishPrompt.svelte';
  import Icon from '$lib/components/Icon.svelte';
  import PendingVerdicts from '$lib/components/PendingVerdicts.svelte';
  import PeoplePicker from '$lib/components/PeoplePicker.svelte';
  import PosterCard, { isColdPlaced } from '$lib/components/PosterCard.svelte';
  import SearchBeyond from '$lib/components/SearchBeyond.svelte';
  import ShelfList from '$lib/components/ShelfList.svelte';
  import TermPicker from '$lib/components/TermPicker.svelte';
  import TitleDetail from '$lib/components/TitleDetail.svelte';
  import LikeFilmsCell from '$lib/components/LikeFilmsCell.svelte';
  import RecipeChips from '$lib/components/RecipeChips.svelte';
  import RecipeGrid from '$lib/components/RecipeGrid.svelte';
  import { startWith } from '$lib/recipe.svelte.js';

  // Back from another tab, the last shelves show at once and are re-read quietly (decision 530).
  const kept = homeKept.user === session.user?.id && homeKept.epoch === modelGate.epoch ? homeKept : null;

  /** A URL's chips with every other filter at its default (decision 557 item 6); its kinds, if named. */
  function fromUrl(next) {
    resetHomeFilters();
    const { kinds: named, ...filters } = next;
    Object.assign(homeFilters, filters);
    return named;
  }

  // The filters outlive the page as Home's place does; a URL that carries any wins over them. Read
  // from `location`: Back leaves `page.url` at the address Home was entered with. An address that
  // still says what Home holds keeps all of it.
  const here = `${location.pathname}${location.search}`;
  const ours =
    homeKept.user === session.user?.id && here === writeHomeUrl(homeFilters, { kinds: homeKept.kinds ?? [] });
  const entry = ours ? null : readHomeUrl(new URLSearchParams(location.search));
  if (!entry && homeKept.user !== session.user?.id) resetHomeFilters();

  // One switch: Films, Series or Both, never neither (decisions 18, 474).
  let kinds = $state((entry && fromUrl(entry)) ?? kept?.kinds ?? ['movie']);

  let items = $state([]);
  let total = $state(0);
  // The quoted matches of an include, which lead the grid (decision 557 item 2); null without one.
  let strongTotal = $state(null);
  // What Only in library leaves out of this grid (decision 558); null while it is off.
  let beyond = $state(null);
  let offset = $state(0);
  let loading = $state(false);
  let facets = $state({ genres: [], decades: [] });
  let selected = $state(null);
  let loadError = $state('');
  let termsOpen = $state(false);
  let peopleOpen = $state(false);
  // An empty grid's chips, each with what dropping it leaves: `{chip, n}`.
  let drops = $state([]);
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

  const mode = $derived(homeMode(homeFilters));
  const reason = $derived(gridReason(homeFilters));
  const nFilters = $derived(activeFilterCount(homeFilters));
  const chips = $derived(chipOrder(homeFilters));
  const rowChips = $derived(homeFilters.panelOpen ? chips.filter((c) => !c.term && !c.person) : chips);
  // Off, a search's grid is still the library: the catalogue and TMDB answer under it (decision 558).
  const scope = $derived(homeFilters.owned || reason === 'search' ? 'only' : 'any');
  // A search's weak tail is folded, not dropped; so is an include's, after its quoted matches.
  const termFold = $derived(reason !== 'search' && homeFilters.terms.some((t) => t.mode === 'in'));
  const cut = $derived.by(() => {
    if (reason === 'search') return strongEnd(items, homeFilters.q) || items.length;
    const weak = termFold ? items.findIndex((t) => t.match === 'weak') : -1;
    return weak === -1 ? items.length : weak;
  });
  const weakItems = $derived(items.slice(cut));
  const strongItems = $derived(items.slice(0, cut));
  // Everything after the last strong hit is weaker, loaded or not: the list is ordered by quality.
  const weakTotal = $derived(weakItems.length ? total - cut : 0);
  const partitioned = $derived(partitionedByKind(kinds, sortEcho));
  // Only in library off, a search answers in three sections (decision 558).
  const beyondSearch = $derived(reason === 'search' && !homeFilters.owned);

  // The shell renders Home's suppressed list; clear it on teardown, or it outlives this surface.
  $effect(() => {
    publishSuppressed(home?.suppressed);
    return () => publishSuppressed([]);
  });

  // A filtered grid counts what it leads with; a search and a filmography say what they are.
  const headLine = $derived(
    reason === 'filter' && strongItems.length
      ? countLabel({ total: strongTotal ?? total, kinds, owned: scope === 'only' })
      : gridLine(reason)
  );

  // The shell's own admin test, so Home and the header agree on who gets the door to Movie data.
  const canAdmin = $derived(
    (session.user?.nav?.account ?? []).some((entry) => entry.key === 'admin')
  );
  const bundleNote = $derived(
    session.hasBundle ? '' : session.restartRequired ? 'waiting for a restart' : 'no movie data yet'
  );
  // The count is the search field's placeholder (decisions 528, 558). With no movie data a count of
  // nothing says nothing: the note stands alone, under the field.
  const placeholder = $derived(
    bundleNote ? 'Search' : searchPlaceholder({ library: home?.library, kinds, owned: homeFilters.owned })
  );
  const note = $derived(bundleNote.replace(/^./, (c) => c.toUpperCase()));

  // The kind switch is the shell's top row; from 721 px the search joins it, as on Rank (decision 554).
  let width = $state(typeof window === 'undefined' ? 390 : window.innerWidth);
  const wide = $derived(width > 720);
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

  function titlesQuery(forKinds, { limit = LIMIT, offset: from = 0, filters = homeFilters } = {}) {
    const { q, ...params } = catalogParams(filters, { owned: scope });
    return `/titles${qs({
      kind: forKinds,
      q,
      ...params,
      // A search is best match first (decision 472) whatever order a filtered grid was put in.
      sort: q ? undefined : (sort ?? undefined),
      limit,
      offset: from
    })}`;
  }

  async function load({ append = false } = {}) {
    const seq = ++requestSeq;
    // A recipe's grid reads for itself; this one reads again once the recipe is gone.
    if (reason === 'recipe') {
      loading = false;
      // The recipe's read names no one, so a person a reload brought is named by an ordinary read.
      if (!append && homeFilters.people.some((p) => !p.name)) {
        get(titlesQuery(kinds, { limit: 1 })).then((res) => takeNames(res.applied)).catch(() => {});
      }
      return;
    }
    const query = titlesQuery(kinds, { offset: append ? offset : 0 });
    const asked = reason;
    loading = true;
    loadError = '';
    if (!append) {
      showWeak = false;
      elsewhere = null;
      drops = [];
      kindNote = '';
    }
    try {
      const res = await get(query);
      if (seq !== requestSeq) return;      // a newer request has already answered
      if (!append) shown = { query, reason: asked };
      items = append ? [...items, ...res.items] : res.items;
      total = res.total;
      strongTotal = res.strong_total ?? null;
      beyond = res.beyond ?? null;
      offset = (append ? offset : 0) + res.items.length;
      // Absent from a build that does not echo it, and then no order control is offered.
      sortEcho = res.sort ?? null;
      forYouAvailable = res.for_you_available ?? null;
      if (append) return;
      takeNames(res.applied);
      if (!res.items.length) findElsewhere(seq, res.hidden ?? {});
      if (!res.items.length || (termFold && strongTotal === 0)) findDrops(seq);
    } catch (err) {
      if (seq !== requestSeq) return;
      // A term the vocabulary no longer has (a stale link) is refused, as a genre is (decision 557).
      const unknown = err?.detail?.reason === 'unknown_term' ? (err.detail.terms ?? []) : [];
      const gone = homeFilters.terms.filter((t) => unknown.includes(t.id));
      if (!gone.length) {
        loadError = err.message;
        return;
      }
      homeFilters.terms = homeFilters.terms.filter((t) => !unknown.includes(t.id));
      load();
      kindNote = termsCleared(gone);
    } finally {
      if (seq === requestSeq) loading = false;
    }
  }

  // Labels and names read from a URL are placeholders until the server names them.
  function takeNames(applied) {
    if (!applied) return;
    const terms = [...(applied.terms ?? []), ...(applied.not_terms ?? [])];
    for (const t of homeFilters.terms) {
      const known = terms.find((a) => a.term === t.id);
      if (known && (known.label !== t.label || known.facet !== t.facet)) {
        Object.assign(t, { label: known.label, facet: known.facet });
      }
    }
    for (const p of homeFilters.people) {
      const known = (applied.people ?? []).find((a) => a.person_ids.join(',') === p.person_ids.join(','));
      if (known && !p.name) Object.assign(p, { name: known.name, person_id: known.person_id, photo: known.photo });
    }
  }

  // Each chip of an empty grid with what dropping it leaves (decision 557 item 7), one count each.
  async function findDrops(seq) {
    const now = $state.snapshot(homeFilters);
    const found = await Promise.all(
      chips
        .filter((chip) => chip.key !== 'owned')
        .map(async (chip) => {
          const filters = { ...now, ...withoutChip(now, chip.key) };
          const res = await get(titlesQuery(kinds, { limit: 1, filters })).catch(() => null);
          return res?.total ? { chip, n: res.total } : null;
        })
    );
    if (seq === requestSeq) drops = found.filter(Boolean);
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

  // A tab's link lands at the top, so Home puts back its kept place; Back restores its own. Only a
  // navigation from Home to Home finds the page already mounted.
  let restoreY = entry ? 0 : (kept?.scrollY ?? 0);
  beforeNavigate(() => {
    homeKept.scrollY = mode === 'shelves' ? window.scrollY : 0;
  });
  afterNavigate(({ type, from, to }) => {
    if (restoreY && type !== 'popstate') window.scrollTo(0, restoreY);
    restoreY = 0;
    if (from && from.route.id === to?.route.id) followAddress();
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
    const { genre, decade } = homeFilters;
    if (found && genre && !found.genres.includes(genre)) {
      dropped.push(genre);
      homeFilters.genre = '';
    }
    if (found && decade && !found.decades.map(String).includes(String(decade))) {
      dropped.push(`${decade}s`);
      homeFilters.decade = '';
    }
    // Load first: a new list clears the old note, then this switch names what it cleared.
    load();
    if (dropped.length) {
      const noun = choice === 'series' ? 'series' : choice === 'movie' ? 'films' : 'titles';
      const them = dropped.length > 1 ? 'them' : 'it';
      kindNote = `${dropped.join(' and ')} cleared — no ${noun} match ${them}.`;
    }
  }

  // Nothing read this grid while a recipe stood in for it, and a recipe may have cleared the search.
  let wasRecipe = false;
  $effect(() => {
    const now = reason === 'recipe';
    if (wasRecipe && !now) untrack(() => load());
    wasRecipe = now;
  });

  let debounce;
  function onQuery() {
    // Clearing the search switches Only in library back on (decision 558 item 4); nothing else does.
    if (!homeFilters.q.trim()) homeFilters.owned = true;
    clearTimeout(debounce);
    debounce = setTimeout(() => load(), 220);
  }

  // The kinds of the shelves an address last took Home off, which a bare address puts back.
  let kindsBefore = null;

  // Home takes what the address says, as a reload would: a card's tap from You sends chips
  // (decision 557 item 6), and Back or the Home tab a bare '/', which is the shelves.
  function followAddress() {
    mirrored = `${location.pathname}${location.search}`;
    const next = readHomeUrl(new URLSearchParams(location.search));
    let named = null;
    if (next) {
      if (mode === 'shelves') kindsBefore = kinds;
      named = fromUrl(next);
    } else if (mode === 'grid' || homeUrl !== '/') {
      resetHomeFilters();
      named = kindsBefore;
      kindsBefore = null;
    } else {
      return;
    }
    if (named && kindChoice(named) !== kindChoice(kinds)) {
      kinds = named;
      loadShelves();
      loadFacets();
    }
    load();
  }

  // Every chip change is mirrored into the address, so a reload and Back hold. Never onto a
  // sheet's own history entry: closing it would take the change back off, so it waits.
  let mirrored = here;
  const homeUrl = $derived(writeHomeUrl(homeFilters, { kinds }));
  $effect(() => {
    const url = homeUrl;
    if (page.state?.sheets?.length || url === mirrored) return;
    mirrored = url;
    replaceState(url, page.state);
  });

  // A jump from another page's card shows the way back to it while its grid stands (board B9); an
  // installed app has no Back of its own. Once its grid has stood and the shelves are back, the jump
  // is over, written to Home's entry when no sheet's entry is on top. State, not derived: the shell
  // draws it, and would re-read a derived of Home's as the page leaves.
  let backTo = $state('');
  let stood = false;
  $effect(() => {
    const from = jumpedFrom();
    const grid = mode === 'grid';
    backTo = grid ? from : '';
    if (!from) stood = false;
    else if (grid) stood = true;
    else if (stood && !page.state.sheets?.length) {
      stood = false;
      const { jumpedFrom: _, ...rest } = page.state;
      replaceState('', rest);
    }
  });

  function removeChip(key) {
    Object.assign(homeFilters, withoutChip(homeFilters, key));
    load();
  }

  function setTerm(term, mode) {
    const chosen = homeFilters.terms.find((t) => t.id === term.term);
    if (chosen) chosen.mode = mode;
    else homeFilters.terms.push({ id: term.term, label: term.label, facet: term.facet, mode });
  }

  function flipTerm(id) {
    const chosen = homeFilters.terms.find((t) => t.id === id);
    if (chosen) chosen.mode = chosen.mode === 'in' ? 'out' : 'in';
    load();
  }

  // One chip per human: a credit folds several person rows (`person_ids`), as the card does.
  function addPerson(person) {
    const ids = person.person_ids?.length ? [...person.person_ids] : [person.person_id ?? person.id];
    if (homeFilters.people.some((p) => p.person_ids.some((id) => ids.includes(id)))) return;
    homeFilters.people.push({
      person_ids: ids, person_id: person.person_id ?? ids[0], name: person.name, photo: person.photo ?? null
    });
  }

  // The title card has closed itself by now, so its history entry is gone (decision 527). Sheets
  // still open under it (a See all, the wish list) close through their own Back before the grid
  // replaces the shelves they stand on, or their entries would hold the mirror and swallow Back.
  async function closeUnder() {
    const depth = page.state?.sheets?.length ?? 0;
    if (depth) await unwind(depth);
  }

  // Clearing the search switches Only in library back on (decision 558 item 4).
  function clearSearch() {
    if (!homeFilters.q.trim()) return;
    homeFilters.q = '';
    homeFilters.owned = true;
  }

  // A tap adds to what is set (decision 557 item 6), and the search makes way for it.
  async function cardPerson(person) {
    await closeUnder();
    addPerson(person);
    clearSearch();
    load();
  }

  async function cardTerm(term) {
    await closeUnder();
    setTerm(term, 'in');
    clearSearch();
    load();
  }

  // The term picker's "Search titles for noir", for a word the vocabulary lacks.
  function searchTitles(text) {
    termsOpen = false;
    homeFilters.q = text;
    load();
  }

  function termsCleared(gone) {
    return `${gone.map((t) => t.label).join(' and ')} cleared — no longer a taste term.`;
  }

  // A recipe's grid holds its own cards, so it hears each toggle as a new `{id, state}`.
  let seenFlip = $state(null);

  // With the seen filter active, a toggled card stops matching and leaves.
  function onSeenChange(titleId, state) {
    seenFlip = { id: titleId, state };
    items = items.map((t) => (t.id === titleId ? { ...t, seen_state: state } : t));
    if (homeFilters.seen !== 'any' && homeFilters.seen !== state) load();
    // The banner is the server's population, so re-read it.
    loadShelves();
  }

  function toggleOwned() {
    homeFilters.owned = !homeFilters.owned;
    load();
  }

  function goBeyond() {
    homeFilters.owned = false;
    load();
  }

  // A sticky notice's x (decision 554): gone at once, then the server's Home; Undo re-reads it.
  async function hideNotice(notice) {
    if (notice === 'pending') home.banner = null;
    else if (notice === 'setup') Object.assign(home, { setup_notice: null, setup_hidden: true });
    else home.wish = { ...home.wish, hidden: true };
    try {
      await putAway(notice, loadShelves);
    } catch (err) {
      showToast(err.message);
    }
    loadShelves();
  }

  // More like this on Home's own card: a new recipe of that title, named at once (decision 559).
  let likedCard = null;
  $effect(() => {
    if (selected) likedCard = selected;
  });
  async function likeOnHome(id) {
    const film = likedCard?.id === id ? likedCard : { id };
    await closeUnder();
    clearSearch();
    startWith(film);
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
    {:else if name === 'ladder'}
      <path d="M7.5 3.5v17M16.5 3.5v17M7.5 8h9M7.5 12.5h9M7.5 17h9" />
    {/if}
  </svg>
{/snippet}

{#snippet searchRow()}
  <div class="searchrow">
    <label class="search">
      {@render icon('search')}
      <input
        type="search"
        data-testid="home-search"
        bind:value={homeFilters.q}
        oninput={onQuery}
        {placeholder}
        aria-label="Search titles"
      />
    </label>
    <button
      class="pill"
      aria-expanded={homeFilters.panelOpen}
      aria-controls={homeFilters.panelOpen ? 'home-filters' : undefined}
      data-testid="filter-toggle"
      onclick={() => (homeFilters.panelOpen = !homeFilters.panelOpen)}
    >{@render icon('filter')}{nFilters ? `Filters · ${nFilters}` : 'Filters'}</button>
  </div>
{/snippet}

{#snippet filterChip(chip)}
  <FilterChip
    variant={chip.variant}
    mode={chip.mode}
    label={chip.label}
    facet={chip.facet}
    person={chip.person}
    testid={chip.testid}
    onFlip={chip.term ? () => flipTerm(chip.term.id) : undefined}
    onRemove={() => removeChip(chip.key)}
  />
{/snippet}

<!-- A picker's cell on a phone: a row that opens its sheet, what it has chosen under it (board B1). -->
{#snippet adder(id, label, open, toggle, chosen)}
  <span class="label" id="{id}-label">{label}</span>
  <button
    class="add"
    id="{id}-add"
    aria-labelledby="{id}-label {id}-add"
    aria-haspopup="dialog"
    aria-expanded={open}
    data-testid="filter-{id}"
    onclick={toggle}
  >Add<Icon name="chevron-right" size={18} /></button>
  {#if chosen.length}
    <div class="picked">{#each chosen as chip (chip.key)}{@render filterChip(chip)}{/each}</div>
  {/if}
{/snippet}

{#snippet termPicker(inline)}
  <TermPicker
    {inline}
    open={termsOpen}
    {kinds}
    chosen={homeFilters.terms}
    onInclude={(term) => (setTerm(term, 'in'), load())}
    onLeaveOut={(term) => (setTerm(term, 'out'), load())}
    onRemove={(term) => removeChip(`term:${term.term}`)}
    onClose={() => (termsOpen = false)}
    onSearchTitles={searchTitles}
  />
{/snippet}

{#snippet peoplePicker(inline, together)}
  <PeoplePicker
    {inline}
    open={peopleOpen}
    {kinds}
    chosen={homeFilters.people}
    combinedLine={together}
    onAdd={(person) => (addPerson(person), load())}
    onRemove={(person) => removeChip(`person:${person.person_ids.join(',')}`)}
    onClose={() => (peopleOpen = false)}
  />
{/snippet}

{#snippet homeBar()}
  <div class="bar">
    {#if backTo}
      <button
        class="btn-plain back"
        aria-label="Back to {backTo}"
        data-testid="home-back"
        onclick={() => history.back()}
      ><Icon name="chevron-left" /><span>{backTo}</span></button>
    {/if}
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

{#if homeFilters.panelOpen}
  {@const n = homeFilters.people.length}
  {@const noun = kinds.length > 1 ? 'Titles' : kinds[0] === 'series' ? 'Series' : 'Films'}
  <!-- What the people alone leave, as their cell's head says it (board B4). -->
  {@const together =
    n > 1 && reason === 'filter' && !gridStale && chips.every((c) => c.variant === 'person')
      ? `${noun} with ${n === 2 ? 'both' : `all ${n}`}: ${total.toLocaleString()} in your library`
      : ''}
  <!-- A cell is a list row on a phone and a label over its control from 721 px (decision 554). -->
  <div class="list-group filterpanel" id="home-filters" data-testid="filter-panel">
    <!-- On a phone the select covers its row, so a tap anywhere on the row opens the native picker. -->
    <div class="cell list-row field">
      <span class="label">Genre</span>
      <span class="value">{homeFilters.genre || 'Any'}{@render icon('chevron')}</span>
      <select bind:value={homeFilters.genre} onchange={() => load()} aria-label="Genre" data-testid="filter-genre">
        <option value="">Any genre</option>
        {#each facets.genres as g (g)}<option value={g}>{g}</option>{/each}
      </select>
    </div>
    <div class="cell list-row field">
      <span class="label">Decade</span>
      <span class="value">{homeFilters.decade ? `${homeFilters.decade}s` : 'Any'}{@render icon('chevron')}</span>
      <select bind:value={homeFilters.decade} onchange={() => load()} aria-label="Decade" data-testid="filter-decade">
        <option value="">Any decade</option>
        {#each facets.decades as d (d)}<option value={d}>{d}s</option>{/each}
      </select>
    </div>
    <div class="cell list-row field">
      <span class="label">Seen</span>
      <span class="value">{SEEN_WORDS[homeFilters.seen]}{@render icon('chevron')}</span>
      <select bind:value={homeFilters.seen} onchange={() => load()} aria-label="Seen state" data-testid="filter-seen">
        <option value="any">Seen or not</option>
        <option value="seen">Seen</option>
        <option value="unseen">Not seen</option>
      </select>
    </div>
    <div class="cell list-row">
      <span class="label" id="owned-label">Only in library</span>
      <button
        class="switch"
        role="switch"
        aria-checked={homeFilters.owned}
        aria-labelledby="owned-label"
        onclick={toggleOwned}
        data-testid="filter-owned"
      ><span class="knob"></span></button>
    </div>
    <!-- From 721 px each picker's cell is its field, its chips in it (boards B1 to B4). -->
    {#if wide}
      <div class="cell half">
        <span class="label">What it's like</span>
        {@render termPicker(true)}
      </div>
      <div class="cell half">
        <div class="cellhead">
          <span class="label">People</span>
          {#if together}<span class="footnote" data-testid="people-combined">{together}</span>{/if}
        </div>
        {@render peoplePicker(true, '')}
      </div>
    {:else}
      <div class="cell list-row adder">
        {@render adder('terms', "What it's like", termsOpen, () => (termsOpen = true), chips.filter((c) => c.term))}
      </div>
      <div class="cell list-row adder">
        {@render adder('people', 'People', peopleOpen, () => (peopleOpen = true), chips.filter((c) => c.person))}
      </div>
    {/if}
    <LikeFilmsCell />
  </div>
  {#if !wide}
    {@render termPicker(false)}
    {@render peoplePicker(false, together)}
  {/if}
{/if}

<!-- Every set filter is a chip (decision 557, board B7); with the panel open a term, a person or a
     recipe's film shows in its own cell instead. -->
{#if rowChips.length || (!homeFilters.panelOpen && (homeFilters.like.length || homeFilters.less.length))}
  <div class="chips" role="group" aria-label="Set filters">
    {#if !homeFilters.panelOpen}<RecipeChips />{/if}
    {#each rowChips as chip (chip.key)}{@render filterChip(chip)}{/each}
  </div>
{/if}

{#if kindNote}
  <p class="footnote kindnote" role="status" data-testid="kind-filter-note">{kindNote}</p>
{/if}

<!-- The notices stand over the shelves; a grid answers one question and shows none (boards A3, A4). -->
{#if mode === 'shelves'}
  <div class="notice-stack">
    <!-- Its answer moves the banner's population, so it re-reads the shelves (decision 212). -->
    <FinishPrompt onAnswered={loadShelves} />
    <PendingVerdicts banner={home?.banner} onHide={() => hideNotice('pending')} />
    <ArrivedBanner arrived={home?.arrived ?? []} onSelect={(title) => (selected = title)} />
    {#if home?.setup_notice}
      {@const notice = home.setup_notice}
      <div class="notice-bar wraps" role="status" data-testid="home-setup-notice">
        <span class="dot" aria-hidden="true">{@render icon('ladder')}</span>
        <div class="text">
          <h2 class="headline">{notice.headline}</h2>
          <p class="line">{notice.why}</p>
        </div>
        <div class="act">
          <a class="pill primary" href={notice.cta.route}>{notice.cta.label}</a>
        </div>
        <button class="x" aria-label="Hide until tomorrow" onclick={() => hideNotice('setup')}>
          {@render icon('close')}
        </button>
      </div>
    {/if}
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
  {#if reason === 'recipe'}<RecipeGrid
      {kinds}
      params={catalogParams()}
      {seenFlip}
      onCleared={(gone) => (kindNote = termsCleared(gone))}
      onSelect={(t) => (selected = t)}
    />{:else}
  {#if beyondSearch}
    <h2 class="section-title" data-testid="library-section-head">In your library</h2>
  {/if}
  <div class="gridhead" data-testid="home-mode" data-mode="grid" data-reason={reason}>
    <div class="headtext">
      {#if headLine}
        <p class="footnote" data-testid="grid-line">{headLine}</p>
      {/if}
      {#if partitioned}
        <p class="footnote" data-testid="grid-partition">{partitionLine(kinds, sortEcho)}</p>
      {/if}
    </div>
    <!-- Offered over what is shown, never over a fold alone. -->
    {#if strongItems.length && sortOffered(reason, sortEcho, forYouAvailable)}
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
    {:else if strongItems.length && sortWaitingLine(reason, sortEcho, forYouAvailable)}
      <p class="footnote" data-testid="sort-waiting">
        {sortWaitingLine(reason, sortEcho, forYouAvailable)}
      </p>
    {/if}
  </div>

  {#if loadError}
    <div class="empty card"><p class="why">{loadError}</p></div>
  {:else if gridStale && shown.reason !== reason}
    <div class="grid" aria-hidden="true">
      {#each { length: 9 }, i (i)}<span class="skeleton ghost"></span>{/each}
    </div>
  {:else}
    <div class="dims" aria-busy={gridStale}>
      {#if !strongItems.length && !loading}
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
            {#if reason === 'search'}
              <!-- A search's grid is the library's part; the rest is beyond it (decision 558). -->
              <p class="why">Nothing in your library matches.</p>
              <!-- Names the dimensions the catalog search really has. -->
              <p class="footnote" data-testid="no-matches-help">
                Search reads titles and their aliases. The kind switch and Filters (genre, decade, seen
                state, Only in library, what it's like, people) narrow it; clear a chip to widen it.
              </p>
            {:else}
              <p class="why" data-testid="no-matches-line">
                {emptyLine({ ...homeFilters, owned: scope === 'only' })}
              </p>
            {/if}
            {#if drops.length || (reason !== 'search' && scope === 'only' && beyond)}
              <!-- Each chip to drop, with what that leaves (decision 557 item 7). -->
              <div class="drops">
                {#each drops as drop (drop.chip.key)}
                  <button class="btn-secondary" data-testid="grid-drop" onclick={() => removeChip(drop.chip.key)}>
                    {dropLabel(drop.chip, drop.n, kinds)}
                  </button>
                {/each}
                {#if reason !== 'search' && scope === 'only' && beyond}
                  <button class="btn-secondary" data-testid="grid-beyond" onclick={goBeyond}>
                    {`${beyond.toLocaleString()} more beyond your library`}
                  </button>
                {/if}
              </div>
            {/if}
          {/if}
        </div>
      {:else}
        {#if (showWeak ? items : strongItems).some(isColdPlaced)}
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
      {/if}
      {#if weakItems.length}
        <!-- A search's hits that only contain the letters, and an include's matches by our read
             alone, wait behind one button (decisions 516, 557). -->
        {#if showWeak}
          <h2 class="list-header weakhead" data-testid="weak-matches-head">
            {#if termFold}
              {strongItems.length ? 'Might also fit' : 'Might fit'}<span class="footnote">Our read · less certain</span>
            {:else}
              Looser matches
            {/if}
          </h2>
          <div class="grid" data-testid="weak-matches">
            {#each weakItems as t (t.id)}
              <PosterCard title={t} onSelect={() => (selected = t)} />
            {/each}
          </div>
        {:else}
          <div class="more">
            <button class="btn-secondary" data-testid="weak-matches-toggle" onclick={() => (showWeak = true)}>
              {termFold
                ? `Show ${weakTotal.toLocaleString()}${strongItems.length ? ' more' : ''} that might fit`
                : `Show ${weakTotal.toLocaleString()} looser ${weakTotal === 1 ? 'match' : 'matches'}`}
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
      {#if !gridStale && homeFilters.owned}
        <!-- Only in library ends a search and a filtered grid on the way past it (decision 558). -->
        {#if reason === 'search'}
          <div class="more beyond">
            <button class="btn-secondary" data-testid="search-beyond" onclick={goBeyond}>
              Search beyond your library
            </button>
            {#if beyond}<span class="footnote">{beyond.toLocaleString()} more in Spielplan</span>{/if}
          </div>
        {:else if beyond && strongItems.length}
          <div class="more beyond">
            <button class="btn-secondary" data-testid="grid-beyond" onclick={goBeyond}>
              {`${beyond.toLocaleString()} more beyond your library`}
            </button>
          </div>
        {/if}
      {/if}
    </div>
  {/if}
  {#if beyondSearch}
    <SearchBeyond {kinds} q={homeFilters.q} params={catalogParams()} onSelect={(t) => (selected = t)} />
  {/if}
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
      onHideWish={() => hideNotice('wish_list')}
    />
  {/if}
{/if}

{#if selected}
  <TitleDetail
    titleId={selected.id}
    seed={selected}
    onClose={() => (selected = null)}
    onPerson={cardPerson}
    onTerm={cardTerm}
    onStateChange={onSeenChange}
    onLike={likeOnHome}
    whyLine={selected.whyLine}
  />
{/if}

<style>
  .bar {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .back {
    flex: none;
    gap: 2px;
    margin: 0 -8px 0 -10px;
    padding: 0 6px 0 4px;
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
  .adder {
    position: relative;
    flex-wrap: wrap;
  }
  .adder > .label {
    min-height: 36px;
    display: flex;
    align-items: center;
  }
  /* On a phone the row's first line opens the picker, its "Add" where a value would sit; the chips
     under it keep their own taps. */
  .adder .add {
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    height: 52px;
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 4px;
    padding: 0 var(--gutter);
    border: none;
    border-radius: 0;
    background: none;
    color: var(--text-3);
    font: inherit;
    cursor: pointer;
  }
  .adder .add:focus-visible {
    outline-offset: -2px;
  }
  .picked {
    flex: 1 0 100%;
    min-width: 0;
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    padding: 4px 0 6px;
  }
  .cellhead {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
  }
  .cellhead .footnote {
    margin: 0;
    text-align: right;
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
  .headtext {
    flex: 1 1 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }
  .sort {
    width: 200px;
    margin-left: auto;
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
  .kindhead,
  .weakhead {
    padding: 0;
  }
  .kindhead {
    grid-column: 1 / -1;
  }
  .weakhead {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 2px 12px;
    margin: 32px 0 12px;
  }
  .weakhead .footnote {
    text-transform: none;
    letter-spacing: normal;
  }
  .more {
    display: flex;
    padding: 24px 0;
  }
  .beyond {
    flex-wrap: wrap;
    align-items: center;
    gap: 8px 16px;
  }
  .more + .beyond {
    padding-top: 0;
  }
  .beyond .footnote {
    margin: 0;
  }
  .drops {
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    gap: 8px;
    margin-top: 8px;
  }
  .drops .btn-secondary {
    margin-top: 0;
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
    .back {
      margin-right: 0;
    }
    .bar .searchrow {
      flex: 0 1 460px;
      min-width: 0;
    }
    .filterpanel {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
    }
    /* Each cell draws the hairlines above and before it; the panel clips the outer ones. */
    .filterpanel > .cell {
      display: flex;
      flex-direction: column;
      align-items: stretch;
      gap: 6px;
      min-height: 0;
      padding: 12px 16px 14px;
      box-shadow: -0.5px -0.5px 0 var(--separator);
    }
    .cell > .label,
    .cellhead > .label {
      font-size: var(--fs-footnote);
      line-height: 18px;
      color: var(--text-3);
    }
    .field:focus-within {
      outline: none;
    }
    .field .value {
      display: none;
    }
    .field select {
      position: static;
      height: 40px;
      min-height: 40px;
      padding: 0 36px 0 12px;
      opacity: 1;
      font-size: var(--fs-subhead);
      line-height: 20px;
    }
    .cell > .switch {
      margin: 4.5px 0;
    }
    .filterpanel > .half {
      grid-column: span 2;
    }
    /* Fixed columns (decision 554 item 3); what a row leaves over goes into its gaps. */
    .grid {
      grid-template-columns: repeat(auto-fill, var(--shelf-poster));
      justify-content: space-between;
      gap: 24px 16px;
    }
  }
</style>
