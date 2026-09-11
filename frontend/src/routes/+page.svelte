<script>
  /**
   * Home. Spec v2.1 §6.0 — M0's catalog and M2's shelves are the same surface in two modes.
   *
   * §6.0 M2: "the default surface becomes personalized shelves over the catalog. A greeting; a
   * pending-verdicts banner … then shelves, each with a mandatory one-line why in vocabulary
   * terms — a shelf that cannot say why it exists doesn't ship."
   *
   * §6.0, the only state machine between the two modes: "Search or an active person-filter
   * switches Home into the catalog grid; clearing it returns the shelves." Both halves matter
   * — the way *out* of the grid is the half nothing else in the spec supplies, which is why the
   * person chip is itself the clear control and why every catalog filter renders as a removable
   * chip beside it (proposals 30 and 152).
   *
   * TWO ROUTES, ON PURPOSE. The shelves half comes from `/api/home` (greeting, banner, shelves,
   * the degraded state, and — only when §6.7's toggle is on — the model annotations and
   * proposal 28's suppressed list); the grid half stays on `/api/titles`, which is the one route
   * carrying M0's five filter dimensions. Asking `/api/home` for the grid would silently drop
   * genre, decade and seen-state.
   *
   * §6.7's rail is NOT mounted here any more. It is one drawer in `routes/+layout.svelte`,
   * beside the toggle that governs it, because mounted here it was reachable from this surface
   * alone [M4.9 finding 25]. All this file still owes it is proposal 28's suppressed list, which
   * no other payload carries.
   */
  import { onMount } from 'svelte';
  import { get, qs } from '$lib/api.js';
  import { session } from '$lib/session.svelte.js';
  import { countLabel, gridReason, homeMode, loadHome, modelGate } from '$lib/home.svelte.js';
  import { publishSuppressed } from '$lib/rail.svelte.js';
  import FinishPrompt from '$lib/components/FinishPrompt.svelte';
  import PendingVerdicts from '$lib/components/PendingVerdicts.svelte';
  import PosterCard from '$lib/components/PosterCard.svelte';
  import ShelfList from '$lib/components/ShelfList.svelte';
  import TitleDetail from '$lib/components/TitleDetail.svelte';

  // Owner decision 18: kind is two independent toggles, either or both active, never neither.
  // A person filter no longer collides with it — turn both on and a filmography is complete.
  let kinds = $state(['movie']);
  let q = $state('');
  let genre = $state('');
  let decade = $state('');
  let seen = $state('any');
  let personId = $state(null);
  let personName = $state('');

  let items = $state([]);
  let total = $state(0);
  let offset = $state(0);
  let loading = $state(false);
  let facets = $state({ genres: [], decades: [] });
  let hidden = $state({});
  let selected = $state(null);
  let loadError = $state('');

  /** @type {any} */
  let home = $state(null);
  let homeLoading = $state(false);
  let homeError = $state('');

  const LIMIT = 60;

  const mode = $derived(homeMode({ q, personId, genre, decade, seen }));
  const reason = $derived(gridReason({ q, personId, genre, decade, seen }));

  // Decision 117 still strips `suppressed` when the toggle is off, so this publishes an absence
  // as readily as a list. The shell renders it: it has no `/api/home` response of its own and
  // must not acquire one for a drawer. The teardown is the load-bearing half — proposal 28's
  // rows say which (shelf, kind) sections did not ship, and left in the drawer on Rate that is
  // a claim about a surface nobody is looking at.
  $effect(() => {
    publishSuppressed(home?.suppressed);
    return () => publishSuppressed([]);
  });

  const activeFilters = $derived(
    [
      genre ? `genre ${genre}` : null,
      decade ? `${decade}s` : null,
      seen !== 'any' ? seen : null
    ].filter(Boolean)
  );
  const count = $derived(countLabel({ total, hidden, kinds, filters: activeFilters }));

  // §2's TZ and proposal 22's four bands are the server's answer; the local one is only a
  // placeholder for the frame before `/api/home` lands.
  const greeting = $derived(
    home?.greeting?.text ??
      `${fallbackBand()}${session.user ? `, ${session.user.name}` : ''}`
  );

  function fallbackBand() {
    const h = new Date().getHours();
    if (h < 5) return 'Up late';
    if (h < 12) return 'Good morning';
    if (h < 18) return 'Good afternoon';
    return 'Good evening';
  }

  // Filter changes fire overlapping requests; without a sequence number a slow earlier
  // response lands after a fast later one and the grid shows the wrong filter's results.
  let requestSeq = 0;

  async function load({ append = false } = {}) {
    const seq = ++requestSeq;
    loading = true;
    loadError = '';
    try {
      const res = await get(
        `/titles${qs({
          kind: kinds,
          q,
          genre,
          decade: decade || undefined,
          seen: seen === 'any' ? undefined : seen,
          person_id: personId ?? undefined,
          limit: LIMIT,
          offset: append ? offset : 0
        })}`
      );
      if (seq !== requestSeq) return;      // a newer request has already answered
      items = append ? [...items, ...res.items] : res.items;
      total = res.total;
      hidden = res.hidden ?? {};
      offset = (append ? offset : 0) + res.items.length;
    } catch (err) {
      if (seq === requestSeq) loadError = err.message;
    } finally {
      if (seq === requestSeq) loading = false;
    }
  }

  let homeSeq = 0;

  /** The shelves half. Depends on the kind selection and on nothing else — which is why
   *  typing into search does not refetch it, and why coming back out of the grid is instant. */
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

  async function loadFacets() {
    facets =
      (await get(`/facets${qs({ kind: kinds })}`).catch(() => null)) ?? { genres: [], decades: [] };
  }

  onMount(async () => {
    await Promise.all([load(), loadFacets(), loadShelves()]);
  });

  // §6.7's toggle lives in the account dropdown, three components away. When it flips, the
  // shelves have to be re-read: the annotations are not hidden client-side, they are absent
  // from the payload, so the only way to show them is to ask again.
  //
  // Watching `modelGate.epoch` rather than `session.user.show_model` is load-bearing.
  // `setShowModel` sets the local user optimistically and awaits the POST afterwards, so
  // refetching on the local flip races the write and returns the pre-toggle payload — the
  // toggle moves and the annotations never arrive. The chip bumps the epoch once the server has
  // it. (Closing an open drawer on the same flip is the shell's job now — it holds the drawer.)
  //
  // `lastEpoch` is deliberately NOT `$state`: it is the effect's own memory, and a reactive
  // one would be read and written in the same effect, which re-runs it forever.
  let lastEpoch = modelGate.epoch;
  $effect(() => {
    const epoch = modelGate.epoch;
    if (epoch === lastEpoch) return;
    lastEpoch = epoch;
    loadShelves();
  });

  function toggleKind(k) {
    const on = kinds.includes(k);
    // Never neither: turning off the last active toggle would silently mean "everything",
    // which is the unpartitioned query §4.1 rule 5 exists to prevent.
    if (on && kinds.length === 1) return;
    kinds = on ? kinds.filter((x) => x !== k) : [...kinds, k];
    // Proposal 32: "switching it closes any open title card, because the card's tier and
    // ledger weight are per-kind quantities."
    selected = null;
    // The facet vocabulary is scoped to the selection, so a genre that only exists in the
    // kind you just switched off would otherwise stay selected and silently return nothing.
    genre = '';
    decade = '';
    loadFacets();
    load();
    loadShelves();
  }

  let debounce;
  function onQuery() {
    // §6.0: search switches Home into the grid AND closes the open detail card. Done on the
    // keystroke rather than after the debounce, so the shelves do not sit under a query that
    // has already been typed.
    selected = null;
    clearTimeout(debounce);
    debounce = setTimeout(() => load(), 220);
  }

  function filterToPerson(person) {
    // Proposal 30: "Tapping a credit navigates to Home, closes the title card, and clears the
    // search box; the person chip is itself the clear control. Matching is by `person.id`."
    personId = person.person_id ?? person.id;
    personName = person.name;
    selected = null;
    q = '';
    load();
  }

  // The grid holds `seen_state` per card, so a toggle on the open panel has to reach it — and
  // when the seen filter is active the card has just stopped matching, so it leaves.
  function onSeenChange(titleId, state) {
    items = items.map((t) => (t.id === titleId ? { ...t, seen_state: state } : t));
    if (seen !== 'any' && seen !== state) load();
    // A verdict-less title that just became `seen` belongs in §6.0's banner, and one that
    // stopped being seen leaves it. The banner is the server's population, so re-read it.
    loadShelves();
  }

  function clearPerson() {
    personId = null;
    personName = '';
    load();
  }

  function clearFilter(which) {
    if (which === 'genre') genre = '';
    if (which === 'decade') decade = '';
    if (which === 'seen') seen = 'any';
    load();
  }
</script>

<!-- §7.3: the queued finish prompt, above everything, because it is about the thing that
     just happened in the living room. Proposal 150 keeps it separate from the banner below:
     one title, armed by playback, and its first tap is what writes `seen`.

     `onAnswered` is decision 212. The prompt's write is the same write the title card makes, and
     that one has re-read the shelves since M2 for a reason stated twenty lines up: a verdict-less
     title that just became `seen` belongs in §6.0's banner, and one that stopped being seen leaves
     it. Mounted with no props, this card emptied its own queue and left the banner below it stale —
     including the banner it had just added a title to. `loadShelves()` and not `onSeenChange`
     deliberately: the grid is only on screen under a search or a filter (§6.0's mode machine), and
     the banner is the population this answer actually moved. -->
<FinishPrompt onAnswered={loadShelves} />

<div class="head">
  <div class="greetline">
    <h1 data-testid="home-greeting" data-band={home?.greeting?.band ?? ""}>{greeting}</h1>
  </div>

  <!-- §6.0's pending-verdicts banner. Server copy, server link. -->
  <PendingVerdicts banner={home?.banner} />

  <div class="controls">
    <div class="kinds" role="group" aria-label="Kind">
      <!-- Two toggles, either or both, never neither. On the shelves this is §4.1 rule 5's
           partition; on the catalog grid, which merely lists in a kind-independent order, it
           is only a filter — decision 18 permits the grid to interleave. -->
      <button
        class="pill"
        data-testid="kind-movie"
        aria-pressed={kinds.includes('movie')}
        onclick={() => toggleKind('movie')}
        title={kinds.length === 1 && kinds[0] === 'movie' ? 'at least one kind stays on' : ''}
      >Films</button>
      <button
        class="pill"
        data-testid="kind-series"
        aria-pressed={kinds.includes('series')}
        onclick={() => toggleKind('series')}
        title={kinds.length === 1 && kinds[0] === 'series' ? 'at least one kind stays on' : ''}
      >Series</button>
    </div>

    <input
      type="search"
      data-testid="home-search"
      bind:value={q}
      oninput={onQuery}
      placeholder="search title, alias"
      aria-label="Search titles"
    />
  </div>

  <div class="filters">
    <select bind:value={genre} onchange={() => load()} aria-label="Genre" data-testid="filter-genre">
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
    {#if personId}
      <!-- Proposal 30: the chip IS the clear control, and it is the only way back out of a
           filmography — so it is always visible and always removable. -->
      <button class="pill on" onclick={clearPerson} data-testid="person-chip">{personName} ✕</button>
    {/if}
    {#if genre}
      <button class="pill on" onclick={() => clearFilter('genre')} data-testid="genre-chip">{genre} ✕</button>
    {/if}
    {#if decade}
      <button class="pill on" onclick={() => clearFilter('decade')} data-testid="decade-chip">{decade}s ✕</button>
    {/if}
    {#if seen !== 'any'}
      <button class="pill on" onclick={() => clearFilter('seen')} data-testid="seen-chip">{seen} ✕</button>
    {/if}
  </div>

  <div class="data count" data-testid="count-line">
    {count}{session.hasBundle ? '' : ' · no bundle imported'}
  </div>
</div>

{#if home?.degraded && home.degraded.state !== 'no_bundle'}
  <!-- Proposal 20's two first-week states. Neither is an error, and neither is optional.

       `no_bundle` is deliberately NOT rendered here. §3.1's no-bundle state already has a
       panel further down — M0's, with the copy the first-boot spec asserts — and it carries the
       same "Import a bundle" CTA to the same route. Rendering both put two identical links on
       one screen: not merely untidy, but a page that says the same thing twice and a locator
       that cannot resolve. The server still sends the state, and `zero_verdicts` (which has no
       older panel) is what this one is for. -->
  <div class="card degraded" data-testid="home-degraded" data-state={home.degraded.state}>
    <h2>{home.degraded.headline}</h2>
    <p class="why">{home.degraded.why}</p>
    {#if home.degraded.cta}
      <a class="btn-primary" href={home.degraded.cta.route}>{home.degraded.cta.label}</a>
    {/if}
  </div>
{/if}

{#if mode === 'grid'}
  <div class="modeline data" data-testid="home-mode" data-mode="grid" data-reason={reason}>
    {`catalog grid · ${reason === 'person' ? 'filmography' : reason === 'search' ? 'search' : 'filtered'} · clear it to return to your shelves`}
  </div>

  {#if loadError}
    <div class="empty card"><p>{loadError}</p></div>
  {:else if !items.length && !loading}
    <div class="empty card">
      {#if !session.hasBundle}
        <h2>Nothing to show yet</h2>
        <p class="why">
          No artifact bundle has been imported. That is a legal state — the app runs, the setup
          wizard and admin routes work, and every artifact-dependent surface says so instead of
          erroring.
        </p>
        <a class="btn-primary" href="/admin/data">Import a bundle</a>
      {:else}
        <h2>No matches</h2>
        <!-- [M4.9 finding 19] This said "try a DNA term — cosy, dread, slow-burn — or check the
             Map's compositional search", proposal 23's copy, adopted from the prototype ahead of
             the routes it describes. `list_titles` has no `dna` parameter at all and its search
             predicate is `lower(t.name) LIKE` or the same over `title_alias`; the Map is §6.4
             and renders M6's placeholder. The one screen whose job is to rescue a failed search
             was sending people to two dead ends, so it now names the dimensions §6.0's M0
             catalog really has, in §6.8's quiet register. Making `q` search DNA terms instead is
             not the fix — §6.4 is where compositional search is specified, and M6 owns it. -->
        <p class="why">Nothing in the library matches.</p>
        <p class="data" data-testid="no-matches-help">
          search reads the title and its aliases · the kind toggles, genre, decade and seen
          state narrow it further · clear a chip to widen it
        </p>
      {/if}
    </div>
  {:else}
    <div class="grid">
      {#each items as t (t.id)}
        <PosterCard title={t} onSelect={() => (selected = t.id)} />
      {/each}
    </div>
    {#if offset < total}
      <div class="more">
        <button class="btn-ghost" onclick={() => load({ append: true })} disabled={loading}>
          {loading ? 'Loading…' : `Show more · ${(total - offset).toLocaleString()} left`}
        </button>
      </div>
    {/if}
  {/if}
{:else if !session.hasBundle}
  <div class="empty card">
    <h2>Nothing to show yet</h2>
    <p class="why">
      No artifact bundle has been imported. That is a legal state — the app runs, the setup
      wizard and admin routes work, and every artifact-dependent surface says so instead of
      erroring.
    </p>
    <a class="btn-primary" href="/admin/data">Import a bundle</a>
  </div>
{:else}
  <div class="modeline data" data-testid="home-mode" data-mode="shelves">
    {home?.shelves_total ?? 0} shelves · each one says why it exists
  </div>
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
  .controls input {
    flex: 1;
    min-width: 200px;
    max-width: 420px;
  }
  .filters {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
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
  .count {
    letter-spacing: 0.04em;
  }
  .modeline {
    margin-bottom: 14px;
    letter-spacing: 0.04em;
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
    /* A centred message with one CTA, not a row in a stack — and it shares this slot
       with ShelfList's own empty state, so the two are one box or they are a bug. */
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
