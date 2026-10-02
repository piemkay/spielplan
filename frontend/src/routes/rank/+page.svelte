<script>
  // A tier list of posters (decision 528): a tap opens the title card, and a drag or the card's tier
  // sheet moves a title. The board is never re-sorted here: a drop waits and the response replaces it.
  import { onDestroy, onMount, tick } from 'svelte';
  import { flip } from 'svelte/animate';
  import { cubicOut } from 'svelte/easing';
  import FilterChip from '$lib/components/FilterChip.svelte';
  import Icon from '$lib/components/Icon.svelte';
  import RateBattleCard from '$lib/components/RateBattleCard.svelte';
  import RatePeek from '$lib/components/RatePeek.svelte';
  import RatePoster from '$lib/components/RatePoster.svelte';
  import Sheet from '$lib/components/Sheet.svelte';
  import TermPicker from '$lib/components/TermPicker.svelte';
  import TitleDetail from '$lib/components/TitleDetail.svelte';
  import { returningCard } from '$lib/cardJump.js';
  import { modelGate } from '$lib/home.svelte.js';
  import { flipFrom, ms, still } from '$lib/motion.js';
  import { session } from '$lib/session.svelte.js';
  import { topbar } from '$lib/topbar.svelte.js';
  import {
    KIND_LABELS,
    ROUND_END_TEXT,
    ROUND_END_TITLE,
    ROUND_SIZE,
    answer,
    chooseKind,
    clearFilter,
    clearFilters,
    closeQueue,
    closeTitle,
    dnaTierText,
    draft,
    emptyState,
    facets,
    filterChips,
    flipTerm,
    includeLabels,
    keepGoing,
    load,
    loadFacets,
    move,
    neighboursAt,
    nounFor,
    openQueue,
    openTitle,
    rank,
    reset,
    roundLine,
    searchHint,
    setTerm,
    showAll,
    showLess,
    spot,
    stays,
    tierLegend,
    typed
  } from '$lib/rank.svelte.js';

  const showModel = $derived(!!session.user?.show_model);
  const empty = $derived(emptyState());
  const chips = $derived(filterChips());
  const termChips = $derived(chips.filter((chip) => chip.term));
  const filtersLabel = $derived(chips.length ? `Filters · ${chips.length}` : 'Filters');
  const legend = $derived(tierLegend());
  let termsOpen = $state(false);

  let width = $state(typeof window === 'undefined' ? 390 : window.innerWidth);
  const wide = $derived(width > 720);

  // A desktop's poster size, remembered on the device.
  const SIZES = { small: 72, medium: 94, large: 124 };
  const SIZE_KEY = 'rank-poster-size';
  let size = $state(savedSize());
  function savedSize() {
    try {
      const saved = localStorage.getItem(SIZE_KEY);
      return saved && Object.hasOwn(SIZES, saved) ? saved : 'medium';
    } catch {
      return 'medium';
    }
  }
  function pickSize(next) {
    size = next;
    try {
      localStorage.setItem(SIZE_KEY, next);
    } catch {
      // Private mode: the choice lasts the visit.
    }
  }

  let gridWidth = $state(0);
  const cols = $derived(wide ? Math.max(1, Math.floor((gridWidth + 8) / (SIZES[size] + 8))) : 4);

  let filtersOpen = $state(false);
  let searchOpen = $state(false);
  let searchInput = $state();
  const searching = $derived(searchOpen || !!draft.q);

  async function openSearch() {
    searchOpen = true;
    await tick();
    searchInput?.focus();
  }
  function closeSearch() {
    searchOpen = false;
    if (draft.q) {
      draft.q = '';
      load(rank.kind);
    }
  }

  // The comparison round answers as Rate's pairs do; a poster only shows the film (decision 528).
  let queuePeek = $state(null);
  const pairSide = (p, s) => ({
    id: p[`title_${s}`],
    name: p[`name_${s}`],
    kind: p[`kind_${s}`],
    year: p[`year_${s}`],
    runtime_min: p[`runtime_min_${s}`]
  });
  const queueCard = $derived(
    rank.pair && {
      token: rank.pair.token,
      reason: rank.pair.reason,
      left: { ...pairSide(rank.pair, 'a'), outcome: 'A' },
      right: { ...pairSide(rank.pair, 'b'), outcome: 'B' }
    }
  );
  // The banner keeps the count it had when the round opened; closing lets the new one re-enter.
  let held = $state(null);
  const straddling = $derived(held ?? rank.straddling);
  function sharpen() {
    held = rank.straddling;
    openQueue();
  }
  function endRound() {
    held = null;
    closeQueue();
  }
  function open(entry) {
    if (suppressClick) return;
    openTitle(entry);
  }

  onMount(() => {
    // Two rows' worth of each tier, generously: the grid measures its columns only once drawn.
    rank.perTier = window.innerWidth > 720 ? 2 * Math.ceil(window.innerWidth / 80) : 8;
    // Back from Home's grid reopens the card the jump there left from (decision 557 item 6).
    const back = returningCard('rank');
    if (back !== null) openTitle({ title_id: back });
    loadFacets('movie');
    return load('movie');
  });
  onDestroy(() => {
    cancel();
    reset({ board: false });
  });

  let lastModelEpoch = modelGate.epoch;
  $effect(() => {
    const epoch = modelGate.epoch;
    if (epoch === lastModelEpoch) return;
    lastModelEpoch = epoch;
    load(rank.kind);
  });

  // The kind switch and the search are the shell's top row here, beside You (decision 528).
  $effect(() => {
    if (!topbar.host) return;
    topbar.content = rankBar;
    return () => {
      if (topbar.content === rankBar) topbar.content = null;
    };
  });

  const countOf = (tier) => tier.count ?? tier.entries.length;

  /** A closed tier shows two rows: its first posters, then "+N" when more follow. */
  function shownOf(tier) {
    if (rank.expanded.includes(tier.index) || countOf(tier) <= 2 * cols) return tier.entries;
    return tier.entries.slice(0, 2 * cols - 1);
  }

  function labelOf(entry) {
    const matched = rank.dnaTiers?.[entry.title_id];
    return [
      entry.name,
      entry.year,
      entry.straddle != null && 'between two tiers',
      matched && dnaTierText(matched, includeLabels())
    ]
      .filter(Boolean)
      .join(', ');
  }

  // --- the tier strip (phones) ---------------------------------------------------------------

  let strip = $state();
  let inView = $state(null);
  const current = $derived(inView ?? rank.tiers[0]?.index ?? null);

  function onScroll() {
    if (wide || !strip) return;
    // A tier is in view once its head reaches the strip's foot, where a jump lands it.
    const line = strip.getBoundingClientRect().bottom + 12;
    let seen = rank.tiers[0]?.index ?? null;
    for (const tier of rank.tiers) {
      const top = document.getElementById(`tier-${tier.index}`)?.getBoundingClientRect().top;
      if (top !== undefined && top <= line) seen = tier.index;
    }
    inView = seen;
  }

  function jump(index) {
    document.getElementById(`tier-${index}`)?.scrollIntoView({ block: 'start', behavior: still() ? 'auto' : 'smooth' });
    inView = index;
  }

  // --- drag (decision 528) --------------------------------------------------------------------
  // `at` counts the target tier's posters without the lifted one; null is the tier alone, which
  // names no neighbour. `x` is null for a keyboard lift.

  /** @type {any} */
  let drag = $state(null);
  /** @type {any} a pointer down on a poster, before it lifts */
  let press = null;
  let pressing = $state(null);
  let suppressClick = false;
  let live = $state('');
  let opener;
  let frame;
  let scrolledAt = 0;
  let board = $state();
  let clone = $state();

  // Cells reflow at once under a live finger, so aim() never reads a moving cell; the slot never glides.
  const glide = (cell) => ({
    duration: cell.entry && !(drag?.x != null && !drag.settling) ? ms(200) : 0,
    easing: cubicOut
  });

  function cellsOf(tier) {
    const lifted = drag?.entry.title_id;
    let n = 0;
    const cells = shownOf(tier)
      // Released, it rests in the slot until the board says where it sits.
      .filter((entry) => !(drag?.settling && drag.at !== null && entry.title_id === lifted))
      .map((entry) => ({
        key: entry.title_id,
        entry,
        at: entry.title_id === lifted ? n : n++
      }));
    const home = drag && drag.tier === drag.entry.tier && drag.at === drag.origin;
    if (drag && drag.tier === tier.index && drag.at !== null && !home) {
      const before = cells.findIndex((c) => c.entry.title_id !== lifted && c.at === drag.at);
      const i = before < 0 ? cells.length : before;
      const col = i % cols;
      const { chip } = spot(tier.label, neighboursAt(tier.index, drag.entry, drag.at));
      const align = col === 0 ? 'start' : col === cols - 1 ? 'end' : '';
      // Released and not flown in by the clone, the slot shows the poster itself.
      const resting = drag.settling && !drag.flying ? drag.entry : null;
      cells.splice(i, 0, { key: 'slot', chip, align, settling: !!drag.settling, resting });
    }
    return { cells, others: n };
  }

  function whereNow() {
    const tier = rank.tiers.find((t) => t.index === drag.tier);
    return spot(tier?.label ?? '', neighboursAt(drag.tier, drag.entry, drag.at)).said;
  }

  function lift(entry, pointer = null) {
    const entries = rank.tiers.find((t) => t.index === entry.tier)?.entries ?? [];
    const origin = entries.findIndex((e) => e.title_id === entry.title_id);
    drag = { entry, tier: entry.tier, at: origin, origin, x: null, y: null, ...pointer };
    scrolledAt = 0;
    if (pointer) frame = requestAnimationFrame(autoscroll);
  }

  function down(entry, event) {
    if (event.button !== 0 || rank.busy || drag || !rank.setUp) return;
    const box = event.currentTarget.getBoundingClientRect();
    press = {
      entry,
      x: event.clientX,
      y: event.clientY,
      dx: event.clientX - box.left,
      dy: event.clientY - box.top,
      w: box.width,
      touch: event.pointerType !== 'mouse'
    };
    if (press.touch) {
      press.timer = setTimeout(() => begin(press.x, press.y), 400);
      press.charge = setTimeout(() => (pressing = entry.title_id), 120);
    }
  }

  function begin(x, y) {
    const { entry, dx, dy, w } = press;
    pressing = null;
    lift(entry, { x, y, dx, dy, w });
  }

  function unpress() {
    clearTimeout(press?.timer);
    clearTimeout(press?.charge);
    press = null;
    pressing = null;
  }

  function pointerMove(event) {
    if (press && !drag) {
      const far = Math.hypot(event.clientX - press.x, event.clientY - press.y);
      // A finger that moves before the long press is a scroll; a mouse must move to drag.
      if (press.touch && far > 8) unpress();
      if (!press || press.touch || far <= 4) return;
      begin(event.clientX, event.clientY);
    }
    if (drag?.x == null || drag.settling) return;
    drag.x = event.clientX;
    drag.y = event.clientY;
    aim(event.clientX, event.clientY);
  }

  function pointerUp() {
    unpress();
    if (drag?.x == null || drag.settling) return;
    // The click that follows a drag is not a tap on the poster.
    suppressClick = true;
    setTimeout(() => (suppressClick = false));
    finish();
  }

  function cancel() {
    unpress();
    if (!drag?.settling) stop();
  }

  function stop() {
    clearTimeout(opener);
    cancelAnimationFrame(frame);
    drag = null;
  }

  function aim(x, y) {
    const el = document.elementFromPoint(x, y);
    if (!drag || !el || el.closest('.slot')) return;
    const letter = el.closest('[data-drop-tier]');
    const cell = el.closest('[data-at]');
    const section = el.closest('[data-tier-index]');
    let tier = null;
    let at = null;
    if (letter) {
      tier = Number(letter.getAttribute('data-drop-tier'));
    } else if (cell && section) {
      tier = Number(section.getAttribute('data-tier-index'));
      const box = cell.getBoundingClientRect();
      const after = !cell.hasAttribute('data-fixed') && x > box.left + box.width / 2;
      at = Number(cell.getAttribute('data-at')) + (after ? 1 : 0);
    } else if (section) {
      tier = Number(section.getAttribute('data-tier-index'));
      at = tier === drag.tier ? drag.at : null;
    }
    drag.hold = !!letter;
    if (tier !== drag.tier) {
      clearTimeout(opener);
      const target = rank.tiers.find((t) => t.index === tier);
      if (target && shownOf(target).length < countOf(target)) {
        opener = setTimeout(() => showAll(target.index), 600);
      }
    }
    drag.tier = tier;
    drag.at = at;
  }

  // The viewport's top and bottom 48 px scroll the page while a poster is held there: faster the
  // deeper it goes, up to 0.75 px a millisecond, whatever the display's refresh rate.
  function autoscroll(now) {
    if (drag?.x == null) return;
    const depth = drag.hold ? 0 : drag.y < 48 ? drag.y - 48 : Math.max(0, drag.y - window.innerHeight + 48);
    const dt = Math.min(now - (scrolledAt || now), 50);
    scrolledAt = now;
    if (depth) {
      const px = Math.max(1, Math.round((Math.min(Math.abs(depth), 48) / 48) * 0.75 * dt));
      window.scrollBy(0, Math.sign(depth) * px);
      aim(drag.x, drag.y);
    }
    frame = requestAnimationFrame(autoscroll);
  }

  async function finish() {
    const { entry, tier, at } = drag;
    const { above, below } = neighboursAt(tier, entry, at);
    const ids = [above?.title_id ?? null, below?.title_id ?? null];
    if (tier === null || stays(entry, tier, ...ids)) return stop();
    clearTimeout(opener);
    cancelAnimationFrame(frame);
    drag.settling = true;
    drag.flying = drag.x != null && !still();
    const moved = move(entry, tier, ...ids);
    await tick();
    const slot = board?.querySelector('.slot')?.getBoundingClientRect();
    if (slot && drag.flying) land(slot);
    else drag.flying = false;
    const [ok] = await Promise.all([moved, new Promise((done) => setTimeout(done, ms(200)))]);
    const from = (clone ?? board?.querySelector('.slot'))?.getBoundingClientRect();
    stop();
    await tick();
    if (ok) flipFrom(board?.querySelector(`[data-title="${entry.title_id}"]`)?.parentElement, from, 240);
  }

  function land(slot) {
    const to = `translate(${slot.left - (drag.x - drag.dx)}px, ${slot.top - (drag.y - drag.dy)}px) rotate(0deg)`;
    clone?.animate?.(
      [
        { transform: 'translate(0px, 0px) rotate(2deg) scale(1.06)' },
        { transform: `${to} scale(0.985)`, offset: 0.75 },
        { transform: `${to} scale(1)`, boxShadow: 'none' }
      ],
      { duration: 200, easing: 'cubic-bezier(0.2, 0.8, 0.2, 1)', fill: 'forwards' }
    );
  }

  // Space lifts and drops, the arrows move through the grid and past a tier's edge, Esc cancels.
  function key(entry, event) {
    if (!drag) {
      if (event.key !== ' ' || rank.busy || !rank.setUp) return;
      event.preventDefault();
      lift(entry);
      live = `${entry.name}, lifted. ${whereNow()}`;
      return;
    }
    if (drag.x !== null || drag.entry.title_id !== entry.title_id) return;
    const steps = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -cols, ArrowDown: cols };
    if (event.key === ' ') {
      event.preventDefault();
      finish().then(async () => {
        await tick();
        board?.querySelector(`[data-title="${entry.title_id}"]`)?.focus();
      });
    } else if (event.key === 'Escape') {
      event.preventDefault();
      stop();
      live = `${entry.name}, put back.`;
    } else if (event.key in steps) {
      event.preventDefault();
      step(steps[event.key]);
      live = whereNow();
    }
  }

  function step(delta) {
    const room = (tier) => shownOf(tier).filter((e) => e.title_id !== drag.entry.title_id).length;
    let i = rank.tiers.findIndex((t) => t.index === drag.tier);
    let at = drag.at + delta;
    if (at < 0 && i > 0) {
      i -= 1;
      at = room(rank.tiers[i]);
    } else if (at > room(rank.tiers[i]) && i < rank.tiers.length - 1) {
      i += 1;
      at = 0;
    }
    drag.tier = rank.tiers[i].index;
    drag.at = Math.max(0, Math.min(at, room(rank.tiers[i])));
  }

  // Once a finger has lifted a poster, its moves drag rather than scroll. Not passive, so it can.
  $effect(() => {
    if (!board) return;
    const hold = (event) => {
      if (drag?.x != null && !drag.settling) event.preventDefault();
    };
    board.addEventListener('touchmove', hold, { passive: false });
    return () => board.removeEventListener('touchmove', hold);
  });
</script>

<svelte:window
  bind:innerWidth={width}
  onscroll={onScroll}
  onpointermove={pointerMove}
  onpointerup={pointerUp}
  onpointercancel={cancel}
  onkeydown={(e) => e.key === 'Escape' && drag?.x != null && cancel()}
/>

{#snippet icon(name)}
  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    {#if name === 'search'}
      <circle cx="11" cy="11" r="6.5" /><path d="m16 16 4.5 4.5" />
    {:else if name === 'filter'}
      <path d="M4 7h10M18 7h2M4 17h4M12 17h8" /><circle cx="16" cy="7" r="2" /><circle cx="10" cy="17" r="2" />
    {:else if name === 'small'}
      {#each [3.75, 10.5, 17.25] as y (y)}
        {#each [3.75, 10.5, 17.25] as x (x)}<rect {x} {y} width="3" height="3" rx="0.75" />{/each}
      {/each}
    {:else if name === 'medium'}
      <rect x="4" y="4" width="6" height="6" rx="1.25" /><rect x="14" y="4" width="6" height="6" rx="1.25" />
      <rect x="4" y="14" width="6" height="6" rx="1.25" /><rect x="14" y="14" width="6" height="6" rx="1.25" />
    {:else if name === 'large'}
      <rect x="4" y="5" width="6" height="14" rx="1.25" /><rect x="14" y="5" width="6" height="14" rx="1.25" />
    {/if}
  </svg>
{/snippet}

{#snippet searchField()}
  <label class="search">
    {@render icon('search')}
    <input
      class="q"
      type="search"
      placeholder={searchHint()}
      aria-label="Search your ranking"
      bind:value={draft.q}
      bind:this={searchInput}
      oninput={typed}
      data-testid="rank-filter"
    />
  </label>
{/snippet}

{#snippet rankBar()}
  <div class="top" class:wide>
    <h1 class="sr-only">Rank</h1>
    {#if searching && !wide}
      {@render searchField()}
      <button class="btn-plain" onclick={closeSearch}>Cancel</button>
    {:else}
      <div class="segmented kinds" role="group" aria-label="Kind">
        {#each Object.entries(KIND_LABELS) as [key, label] (key)}
          <button aria-pressed={rank.kind === key} data-kind={key} onclick={() => chooseKind(key)}
            >{label}</button
          >
        {/each}
      </div>
      {#if wide}{@render searchField()}{:else}<span class="grow"></span>{/if}
      {#if !wide}
        <button class="btn-plain icon" aria-label="Search your ranking" data-testid="rank-search" onclick={openSearch}
          >{@render icon('search')}</button
        >
      {/if}
      <button
        class={wide ? 'pill' : 'btn-plain icon'}
        aria-label={wide ? undefined : filtersLabel}
        aria-haspopup="dialog"
        aria-expanded={filtersOpen}
        onclick={() => (filtersOpen = true)}
        data-testid="rank-filters"
      >
        {@render icon('filter')}{#if wide}{filtersLabel}{/if}
      </button>
      {#if wide}
        <span class="grow"></span>
        <div class="segmented sizes" role="group" aria-label="Poster size">
          {#each Object.keys(SIZES) as name (name)}
            <button aria-label="{name[0].toUpperCase()}{name.slice(1)} posters" aria-pressed={size === name} onclick={() => pickSize(name)}
              >{@render icon(name)}</button
            >
          {/each}
        </div>
      {/if}
    {/if}
  </div>
{/snippet}

<section class="rank" class:wide class:dragging={!!drag} data-testid="rank-surface">
  {#if !topbar.host}{@render rankBar()}{/if}

  {#if chips.length}
    <div class="chips">
      {#each chips as chip (chip.key)}
        {#if chip.term}
          <FilterChip
            variant="term"
            mode={chip.term.mode}
            label={chip.text}
            facet={chip.term.facet}
            testid="rank-term-chip"
            onFlip={() => flipTerm(chip.term.id)}
            onRemove={() => clearFilter(chip.key)}
          />
        {:else}
          <button
            class="pill on"
            onclick={() => clearFilter(chip.key)}
            aria-label={`Remove ${chip.text}`}
            data-testid={`rank-filter-chip-${chip.key}`}
          >
            {chip.text}
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18" /></svg>
          </button>
        {/if}
      {/each}
      {#if legend}
        <!-- The mark on a poster our read alone admitted (§4.1 rule 1, decision 557 item 8). -->
        <p class="legend" data-testid="rank-tier-legend"><span class="ours" aria-hidden="true"></span>{legend}</p>
      {/if}
    </div>
  {/if}

  {#if !rank.setUp}
    <div class="card setup" data-testid="rank-setup-card">
      <h2 class="section-title">Set up your ladder.</h2>
      <p class="why">Your list opens for moving and comparing once your ladder is set up. It takes about a minute.</p>
      <a class="btn-primary" href="/rate/setup">Set up my ladder</a>
    </div>
  {/if}

  {#if !wide && rank.tiers.length}
    <nav class="strip" aria-label="Tiers" bind:this={strip}>
      {#each rank.tiers as tier (tier.index)}
        <button
          class:on={!drag && current === tier.index}
          class:lit={drag && drag.tier === tier.index && drag.at === null}
          class:target={!!drag}
          data-drop-tier={rank.setUp ? tier.index : undefined}
          aria-label="{tier.label}, {countOf(tier)} {nounFor(countOf(tier))}"
          aria-current={current === tier.index ? 'true' : undefined}
          onclick={() => jump(tier.index)}
        >
          <span class="l">{tier.label}</span>
          <span class="c">{countOf(tier)}</span>
        </button>
      {/each}
    </nav>
  {/if}

  {#if drag && !wide}
    <p class="look hint">Drop between posters to place it · on a letter to move it there</p>
  {:else if rank.setUp && rank.ratedTotal >= 2}
    <section class="look" aria-label="Needs a look" data-testid="rank-look">
      {#if straddling}<span class="dot" aria-hidden="true"></span>{/if}
      <p class:wrap={!straddling && rank.guessing}>
        {#if straddling}
          {#key straddling}<span class="n">{straddling}</span>{/key}
          {straddling === 1 ? 'title sits' : 'titles sit'} between two tiers
        {:else if rank.guessing}
          The order inside each step is still mostly our guess
        {:else}
          Sharpen your list
        {/if}
      </p>
      <button class="go btn-tinted" onclick={sharpen} disabled={rank.busy} data-testid="rank-sharpen"
        ><span class="hit" aria-hidden="true"></span>Sharpen{#if wide}{` — ${ROUND_SIZE} quick questions`}{/if}</button
      >
    </section>
  {/if}

  {#if rank.error}
    <p class="error" role="alert">{rank.error}</p>
  {/if}
  {#if rank.notice && !rank.queueOpen}
    <p class="footnote" role="status">{rank.notice}</p>
  {/if}
  {#if rank.loading && !rank.tiers.length}
    <div class="waiting" style:--poster="{SIZES[size]}px" data-testid="rank-loading">
      <p class="sr-only" role="status">Loading your list…</p>
      {#if !wide}<span class="skeleton band"></span>{/if}
      <span class="skeleton band"></span>
      {#each [0, 1] as t (t)}
        <span class="skeleton mark"></span>
        <div class="grid">{#each { length: 8 }}<span class="skeleton"></span>{/each}</div>
      {/each}
    </div>
  {/if}

  {#if empty}
    <div class="card empty" data-testid="rank-empty">
      <p class="why">{empty.text}</p>
      {#if empty.kind === 'no-match'}
        <button class="btn-secondary" onclick={clearFilters} data-testid="rank-clear">{empty.cta}</button>
      {:else}
        <a class="btn-secondary" href="/rate">{empty.cta}</a>
      {/if}
    </div>
  {/if}

  <p class="sr-only" aria-live="polite">{live}</p>

  <!-- Empty tiers stay on screen as drop targets. -->
  <div
    class="board"
    class:stale={rank.reading > 0}
    style:--poster="{SIZES[size]}px"
    data-testid="rank-board"
    bind:this={board}
  >
    {#each rank.tiers as tier, i (tier.index)}
      {@const count = countOf(tier)}
      {@const { cells, others } = cellsOf(tier)}
      {@const more = count - shownOf(tier).length}
      <section
        class="tier enter"
        style:--i={i}
        id="tier-{tier.index}"
        aria-labelledby="tier-name-{tier.index}"
        data-tier={tier.label}
        data-tier-index={tier.index}
      >
        <div
          class="head"
          class:lit={drag?.tier === tier.index}
          data-drop-tier={rank.setUp ? tier.index : undefined}
          data-testid="rank-tier-{tier.label}"
        >
          <h2 class="name" id="tier-name-{tier.index}">
            <span class="letter" data-testid="rank-letter-{tier.label}">{tier.label}</span>
            <!-- A custom label is its own word, so it shows once. -->
            {#if tier.word && tier.word !== tier.label}<span>{tier.word}</span>{/if}
          </h2>
          <span class="count">{count} {nounFor(count)}</span>
          {#if rank.expanded.includes(tier.index) && count > 2 * cols}
            <button class="btn-plain less" onclick={() => showLess(tier.index)}>Show less</button>
          {/if}
        </div>
        {#if tier.entries.length}
          <ol class="grid" bind:clientWidth={gridWidth}>
            {#each cells as cell (cell.key)}
              {@const lifted = !!cell.entry && drag?.entry.title_id === cell.entry.title_id}
              <li
                class:ghost={lifted}
                class:slot={!cell.entry}
                class:settling={cell.settling}
                aria-hidden={cell.entry ? undefined : 'true'}
                animate:flip={glide(cell)}
              >
                {#if cell.entry}
                  <button
                    class="tile"
                    class:pressing={pressing === cell.entry.title_id}
                    data-title={cell.entry.title_id}
                    data-at={cell.at}
                    data-fixed={lifted ? '' : undefined}
                    data-testid="rank-open-{cell.entry.title_id}"
                    aria-label={labelOf(cell.entry)}
                    onclick={() => open(cell.entry)}
                    onpointerdown={(e) => down(cell.entry, e)}
                    onkeydown={(e) => key(cell.entry, e)}
                    onkeyup={(e) => e.key === ' ' && e.preventDefault()}
                    onblur={() => drag?.x === null && !drag.settling && stop()}
                    oncontextmenu={(e) => e.preventDefault()}
                  >
                    <RatePoster title={{ id: cell.entry.title_id, name: cell.entry.name }} showName="missing" lazy />
                    {#if cell.entry.straddle != null}<span class="dot" aria-hidden="true"></span>{/if}
                    {#if rank.dnaTiers?.[cell.entry.title_id] === 'projected'}<span class="ours" aria-hidden="true"></span>{/if}
                  </button>
                {:else if cell.resting}
                  <RatePoster title={{ id: cell.resting.title_id, name: cell.resting.name }} showName="missing" />
                {:else if !cell.settling}
                  <span class="where {cell.align}">{cell.chip}</span>
                {/if}
              </li>
            {/each}
            {#if more > 0}
              <li>
                <button
                  class="more"
                  data-at={others}
                  data-fixed=""
                  aria-label="Show all {count} in {tier.label}"
                  data-testid="rank-more-{tier.label}"
                  onclick={() => showAll(tier.index)}
                >
                  <span class="n">+{more}</span>
                  <span>Show all</span>
                </button>
              </li>
            {/if}
          </ol>
        {:else}
          <p class="nothing">Nothing here yet</p>
        {/if}
      </section>
    {/each}
  </div>

  {#if showModel && rank.model}
    <div class="card data model" data-testid="rank-model">
      cutpoints [{rank.model.cutpoints.map((c) => c.toFixed(2)).join(' · ')}] · straddle_z
      {rank.model.straddle_z} · tension {rank.model.tension_credible_mass} · held-out
      {rank.model.held_out.pairs} pairs
      {#if rank.model.held_out.rate !== null}· agreement {rank.model.held_out.rate.toFixed(2)}{/if}
    </div>
  {/if}
  {#if showModel && rank.log.length}
    <ul class="card data log" data-testid="rank-log">
      {#each rank.log as line}<li>{line}</li>{/each}
    </ul>
  {/if}
</section>

{#if drag?.x != null && (!drag.settling || drag.flying)}
  <div
    class="lifted"
    bind:this={clone}
    aria-hidden="true"
    style:left="{drag.x - drag.dx}px"
    style:top="{drag.y - drag.dy}px"
    style:width="{drag.w}px"
  >
    <RatePoster title={{ id: drag.entry.title_id, name: drag.entry.name }} showName="missing" />
  </div>
{/if}

<!-- The bars sit in the content, not the header: the header's grab area captures the pointer. -->
<Sheet open={filtersOpen} onClose={() => (filtersOpen = false)} label="Filters" detent="medium" width={480}>
  {#snippet children(close)}
    <div class="bar">
      <h2 class="section-title">Filters</h2>
      <button class="btn-plain" onclick={close}>Done</button>
    </div>
    <div class="filters" data-testid="rank-filters-sheet">
      <div class="list-group">
        <label class="list-row">
          <span>Genre</span>
          <select bind:value={draft.genre} onchange={() => load(rank.kind)} data-testid="rank-genre">
            <option value="">Any genre</option>
            {#each facets.genres as g (g)}<option value={g}>{g}</option>{/each}
          </select>
        </label>
        <label class="list-row">
          <span>Decade</span>
          <select bind:value={draft.decade} onchange={() => load(rank.kind)} data-testid="rank-decade">
            <option value="">Any decade</option>
            {#each facets.decades as d (d)}<option value={d}>{d}s</option>{/each}
          </select>
        </label>
        <label class="list-row">
          <span>Seen</span>
          <select bind:value={draft.seen} onchange={() => load(rank.kind)} data-testid="rank-seen">
            <option value="any">Seen or not</option>
            <option value="seen">Seen</option>
            <option value="unseen">Not seen</option>
          </select>
        </label>
        <label class="list-row">
          <span>Max length</span>
          <input
            class="value"
            type="number"
            min="1"
            inputmode="numeric"
            placeholder="Any"
            bind:value={draft.runtime_max}
            oninput={typed}
            data-testid="rank-runtime"
          />
          <span class="unit">min</span>
        </label>
        <!-- Home's term picker (decision 557 item 8): in this row on a desktop (board B8), a sheet
             over this one on a phone. -->
        {#if wide}
          {@render termPicker(true)}
        {:else}
          <div class="list-row adder">
            <span id="rank-terms-label">What it's like</span>
            <button
              class="add"
              aria-labelledby="rank-terms-label rank-terms"
              aria-haspopup="dialog"
              onclick={() => (termsOpen = true)}
              id="rank-terms"
              data-testid="rank-terms"
            >Add<Icon name="chevron-right" size={18} /></button>
          </div>
        {/if}
        {#if !wide && termChips.length}
          <div class="list-row picked">
            {#each termChips as chip (chip.key)}
              <FilterChip
                variant="term"
                mode={chip.term.mode}
                label={chip.text}
                facet={chip.term.facet}
                testid="rank-sheet-term-chip"
                onFlip={() => flipTerm(chip.term.id)}
                onRemove={() => clearFilter(chip.key)}
              />
            {/each}
          </div>
        {/if}
      </div>
      {#if chips.length}
        <button class="btn-plain clear" onclick={clearFilters}>Clear all</button>
      {/if}
    </div>
  {/snippet}
</Sheet>

{#snippet termPicker(inline)}
  <TermPicker
    {inline}
    drop="below"
    open={termsOpen}
    kinds={[rank.kind]}
    chosen={draft.terms}
    testid="rank-terms"
    chipTestid="rank-sheet-term-chip"
    onInclude={(term) => setTerm(term, 'in')}
    onLeaveOut={(term) => setTerm(term, 'out')}
    onRemove={(term) => clearFilter(`term:${term.term}`)}
    onClose={() => (termsOpen = false)}
  />
{/snippet}
{#if !wide}{@render termPicker(false)}{/if}

<Sheet open={rank.queueOpen} onClose={endRound} label="Sharpen your list" width={480}>
  {#snippet children(close)}
    <div class="bar">
      <span class="footnote data" data-testid="rank-round">{roundLine()}</span>
      <button class="btn-plain" onclick={close} data-testid="rank-queue-close">Done</button>
    </div>
    <div class="queue" data-testid="rank-queue">
      {#if rank.notice}<p class="footnote" role="status">{rank.notice}</p>{/if}
      {#if rank.roundDone}
        <div class="round-end" data-testid="rank-round-end">
          <h2 class="title-1">{ROUND_END_TITLE}</h2>
          <p class="why">{ROUND_END_TEXT}</p>
          <div class="round-actions">
            <button class="btn-primary" onclick={keepGoing} data-testid="rank-round-more">Keep going</button>
            <button class="btn-secondary" onclick={close} data-testid="rank-round-stop">Done</button>
          </div>
        </div>
      {:else if rank.pair}
        {#if showModel && rank.pair.model}
          <p class="data arm" data-testid="rank-pair-arm">
            {rank.pair.model.arm} · {rank.pair.model.reason}
          </p>
        {/if}
        <RateBattleCard
          card={queueCard}
          busy={rank.busy}
          pending={rank.pending}
          much={false}
          onDuel={(outcome) => answer(outcome)}
          onPeek={(side) => (queuePeek = queueCard[side])}
        />
      {:else}
        <p class="why" data-testid="rank-queue-empty">{rank.queueReason}</p>
      {/if}
      {#if rank.placed.length}
        <!-- Placement, never "moved", whichever arm drew the pair (§13). -->
        <section class="placed" data-testid="rank-placed">
          <h3 class="list-header">Where they sit now</h3>
          <ul class="list-group">
            {#each rank.placed as spot (spot.title_id)}
              <li class="list-row spot" data-testid={`rank-placed-${spot.title_id}`}>
                <span>{spot.name}</span>
                <span class="footnote">{spot.badge}</span>
              </li>
            {/each}
          </ul>
        </section>
      {/if}
    </div>
  {/snippet}
</Sheet>

{#if queuePeek}
  <RatePeek title={queuePeek} onClose={() => (queuePeek = null)} />
{/if}

{#if rank.opened !== null}
  <TitleDetail
    titleId={rank.opened}
    from="rank"
    onClose={closeTitle}
    onStateChange={() => load(rank.kind)}
    onMove={(entry, tier) => move(entry, tier.index, null, null, 'explicit')}
  />
{/if}

<style>
  .rank {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .rank:not(.wide) {
    max-width: 720px;
  }
  .dragging,
  .dragging :global(*) {
    cursor: grabbing;
    -webkit-user-select: none;
    user-select: none;
  }
  .grow {
    flex: 1;
    min-width: 0;
  }

  .top {
    display: flex;
    align-items: center;
    gap: 4px;
    min-height: 44px;
  }
  .top.wide {
    gap: 12px;
    min-height: 64px;
  }
  .top .btn-plain {
    font-weight: 600;
  }
  .kinds {
    flex: none;
    width: 150px;
    min-height: 32px;
  }
  .kinds > button,
  .sizes > button {
    font-size: var(--fs-footnote);
    line-height: 18px;
  }
  .sizes {
    flex: none;
    width: 116px;
    min-height: 32px;
  }
  .sizes > button {
    display: grid;
    place-items: center;
  }
  .sizes svg {
    width: 20px;
    height: 20px;
  }
  .icon {
    width: var(--touch);
    justify-content: center;
    padding: 0;
    color: var(--text);
  }
  .top .pill svg {
    width: 18px;
    height: 18px;
  }
  .search {
    position: relative;
    flex: 1;
    min-width: 0;
    display: flex;
    align-items: center;
    color: var(--text-3);
  }
  .top.wide .search {
    flex: 0 1 320px;
  }
  .search svg {
    position: absolute;
    left: 12px;
    width: 18px;
    height: 18px;
    pointer-events: none;
  }
  /* Class and type both: design.css's input rule is four :not()s deep. */
  .search input.q[type='search'] {
    -webkit-appearance: none;
    appearance: none;
    padding-left: 38px;
  }
  .top.wide .search input.q[type='search'] {
    min-height: 40px;
    font-size: var(--fs-subhead);
  }
  .chips {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }
  .legend {
    display: flex;
    align-items: center;
    gap: 8px;
    margin: 0 0 0 8px;
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-3);
  }
  /* Our read's mark: a ring, apart from the quoted tier's fill (board B8). */
  .ours {
    flex: none;
    display: grid;
    place-items: center;
    width: 18px;
    height: 18px;
    border-radius: var(--r-pill);
    background: var(--surface-2);
  }
  .ours::before {
    content: '';
    width: 8px;
    height: 8px;
    border-radius: var(--r-pill);
    box-shadow: inset 0 0 0 1.5px var(--text);
  }

  /* Phones: the tiers' letters and counts stay in view, and jump to a tier. */
  .strip {
    position: sticky;
    top: env(safe-area-inset-top);
    z-index: 5;
    display: flex;
    gap: 4px;
    margin: 0 calc(-1 * var(--gutter)) -4px;
    padding: 8px var(--gutter) 0;
    background: var(--bg);
    /* Covers the status bar's strip above it once it sticks. */
    box-shadow: 0 calc(-1 * env(safe-area-inset-top)) 0 var(--bg);
  }
  .strip button {
    flex: 1 1 0;
    min-width: 0;
    height: 44px;
    min-height: 44px;
    padding: 0;
    border: none;
    border-radius: var(--r-sm);
    background: var(--surface-1);
    color: var(--text);
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
  }
  .strip .l {
    font-family: var(--serif);
    font-size: var(--fs-section);
    line-height: 22px;
  }
  .strip .c {
    font-size: var(--fs-caption);
    line-height: 14px;
    color: var(--text-3);
    font-variant-numeric: tabular-nums;
  }
  .strip button.on {
    background: var(--text);
    color: var(--bg);
  }
  .strip button.on .c {
    color: rgba(12, 11, 10, 0.6);
  }
  .strip button.target {
    outline: 1px dashed rgba(245, 240, 232, 0.28);
    outline-offset: -1px;
  }
  .strip button.lit {
    outline: none;
    background: var(--accent-tint);
    box-shadow: inset 0 0 0 1.5px var(--accent);
    transform: scale(1.08);
  }

  /* Needs a look: the dots' legend, and the comparison round's way in. */
  .look {
    display: flex;
    align-items: center;
    gap: 10px;
    min-height: 44px;
    margin: 0;
    padding: 0 6px 0 12px;
    border-radius: var(--r-md);
    background: var(--surface-1);
  }
  .look p {
    flex: 1;
    min-width: 0;
    margin: 0;
    font-size: var(--fs-subhead);
    line-height: 20px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .look .n {
    display: inline-block;
    --enter-y: 6px;
    animation: enter 200ms var(--ease);
  }
  .look.hint {
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-2);
  }
  .look p.wrap {
    padding: 12px 0;
    white-space: normal;
  }
  .look .go {
    position: relative;
    flex: none;
    min-height: 32px;
    padding: 0 14px;
    border-radius: var(--r-pill);
    font-size: var(--fs-subhead);
    line-height: 20px;
    font-weight: 600;
  }
  /* The pill draws 32 px; a finger reaches 48. */
  .hit {
    position: absolute;
    inset: -8px 0;
  }
  .setup {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 8px;
  }
  .setup .why {
    margin: 0;
  }
  .setup .btn-primary {
    min-height: var(--touch);
  }
  .dot {
    flex: none;
    width: 8px;
    height: 8px;
    border-radius: var(--r-pill);
    background: var(--warning);
  }

  .error {
    margin: 0;
    color: var(--negative);
    font-size: var(--fs-subhead);
  }
  .empty {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 12px;
  }
  .empty .why {
    margin: 0;
  }

  .waiting {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .waiting .band {
    height: 44px;
    border-radius: var(--r-md);
  }
  .waiting .mark {
    width: 28px;
    height: 28px;
    border-radius: 8px;
  }
  .waiting .grid > span {
    aspect-ratio: 2 / 3;
  }

  .board {
    display: flex;
    flex-direction: column;
    gap: 16px;
    transition: opacity var(--dur-quick) var(--ease);
  }
  .board.stale {
    opacity: 0.6;
    transition-delay: 150ms;
  }
  .tier {
    display: flex;
    flex-direction: column;
    gap: 8px;
    scroll-margin-top: calc(env(safe-area-inset-top) + 60px);
  }
  .head {
    display: flex;
    align-items: center;
    gap: 12px;
    min-height: 36px;
  }
  .name {
    flex: 1;
    min-width: 0;
    margin: 0;
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: var(--fs-footnote);
    line-height: 18px;
    font-weight: 400;
    color: var(--text-3);
  }
  .letter {
    min-width: 28px;
    height: 28px;
    flex: none;
    padding: 0 3px;
    display: grid;
    place-items: center;
    border-radius: 8px;
    background: var(--surface-2);
    font-family: var(--serif);
    font-size: var(--fs-section);
    line-height: 22px;
    color: var(--text);
  }
  .head,
  .letter {
    transition: background 480ms var(--ease), color 480ms var(--ease);
  }
  .head.lit,
  .head.lit .letter {
    transition-duration: var(--dur-quick);
  }
  .head.lit .letter {
    background: var(--accent-tint);
    color: var(--accent-text);
  }
  .count {
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-3);
    font-variant-numeric: tabular-nums;
  }
  .less {
    margin-right: -8px;
    font-size: var(--fs-footnote);
  }
  .grid {
    margin: 0;
    padding: 0;
    list-style: none;
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 6px;
  }
  .tile {
    position: relative;
    display: block;
    width: 100%;
    padding: 0;
    border: none;
    border-radius: var(--r-poster);
    background: none;
    color: inherit;
    -webkit-touch-callout: none;
    -webkit-user-select: none;
    user-select: none;
    transition: transform 0.15s var(--ease);
  }
  .tile.pressing {
    transform: scale(0.96);
    filter: brightness(0.92);
    transition: transform 280ms linear, filter 280ms linear;
  }
  @media (hover: hover) and (pointer: fine) {
    .tile {
      cursor: grab;
    }
    .tile:hover {
      transform: translateY(-2px);
    }
  }
  .tile .dot {
    position: absolute;
    top: 6px;
    right: 6px;
    box-shadow: 0 0 0 2px var(--bg);
  }
  /* Top left: a posterless title's name runs along the foot. */
  .tile .ours {
    position: absolute;
    top: 6px;
    left: 6px;
    background: rgba(12, 11, 10, 0.72);
  }
  .ghost {
    border-radius: var(--r-poster);
    outline: 1.5px dashed rgba(245, 240, 232, 0.28);
    outline-offset: -1.5px;
  }
  .ghost .tile {
    opacity: 0.4;
  }
  .slot {
    position: relative;
    aspect-ratio: 2 / 3;
    border: 1.5px dashed var(--ember-edge);
    border-radius: var(--r-poster);
    background: var(--accent-tint);
    animation: fadeIn 0.2s var(--ease);
    transition: border-color 160ms var(--ease), background 160ms var(--ease);
  }
  .slot.settling {
    border-color: transparent;
    background: transparent;
  }
  .slot.settling::before {
    content: none;
  }
  /* The insertion bar, with a dot at each end, in the gap before the slot. */
  .slot::before {
    content: '';
    position: absolute;
    top: -4px;
    bottom: -4px;
    left: -7px;
    width: 6px;
    background:
      radial-gradient(circle at 50% 3px, var(--accent) 2.5px, transparent 3px),
      radial-gradient(circle at 50% calc(100% - 3px), var(--accent) 2.5px, transparent 3px),
      linear-gradient(var(--accent), var(--accent)) center / 3px 100% no-repeat;
  }
  .where {
    position: absolute;
    bottom: calc(100% + 8px);
    left: 50%;
    z-index: 2;
    max-width: calc(100vw - 2 * var(--gutter));
    height: 24px;
    padding: 0 10px;
    display: flex;
    align-items: center;
    border-radius: var(--r-pill);
    background: var(--surface-3);
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.35);
    font-size: var(--fs-caption);
    line-height: 16px;
    font-weight: 600;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    transform: translateX(-50%);
    pointer-events: none;
  }
  .where.start {
    left: 0;
    transform: none;
  }
  .where.end {
    left: auto;
    right: 0;
    transform: none;
  }
  .more {
    width: 100%;
    aspect-ratio: 2 / 3;
    padding: 0;
    border: none;
    border-radius: var(--r-poster);
    background: var(--surface-2);
    color: var(--text-3);
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 2px;
    font-size: var(--fs-caption);
    line-height: 16px;
  }
  .more .n {
    color: var(--text);
    font-size: var(--fs-body);
    line-height: 22px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }
  .nothing {
    margin: 0;
    min-height: 52px;
    display: flex;
    align-items: center;
    padding: 0 16px;
    border-radius: var(--r-md);
    background: var(--surface-1);
    font-size: var(--fs-subhead);
    line-height: 20px;
    color: var(--text-3);
  }
  .lifted {
    position: fixed;
    z-index: 80;
    border-radius: var(--r-poster);
    box-shadow: var(--shadow-menu);
    transform: rotate(2deg) scale(1.06);
    pointer-events: none;
    animation: lift 160ms var(--ease-spring) both;
  }
  @keyframes lift {
    from {
      transform: none;
      box-shadow: none;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .lifted,
    .strip button.lit,
    .tile:hover,
    .tile.pressing {
      transform: none;
    }
  }

  /* Desktop: each tier a band, its letter cell beside a wrapping grid. */
  .wide .board {
    gap: 12px;
  }
  .wide .tier {
    flex-direction: row;
    gap: 12px;
    padding: 8px;
    border-radius: var(--r-md);
    background: var(--surface-1);
  }
  .wide .head {
    width: 96px;
    flex: none;
    flex-direction: column;
    align-items: center;
    justify-content: flex-start;
    gap: 0;
    padding: 20px 8px;
    border-radius: var(--r-sm);
    background: var(--surface-2);
    text-align: center;
  }
  .wide .head.lit {
    background: var(--accent-tint);
    box-shadow: inset 0 0 0 1px var(--ember-edge);
  }
  .wide .name {
    flex: none;
    flex-direction: column;
    gap: 4px;
    font-size: var(--fs-caption);
    line-height: 16px;
    text-wrap: balance;
  }
  .wide .letter {
    height: auto;
    background: none;
    font-size: var(--fs-display);
    line-height: 48px;
  }
  .wide .head.lit .letter {
    background: none;
  }
  .wide .count {
    font-size: var(--fs-caption);
    line-height: 16px;
  }
  .wide .less {
    margin: 4px 0 0;
    font-size: var(--fs-caption);
  }
  .wide .grid {
    flex: 1;
    min-width: 0;
    grid-template-columns: repeat(auto-fill, var(--poster));
    gap: 8px;
    align-content: start;
  }
  .wide .nothing {
    flex: 1;
    background: none;
  }

  .model,
  .log {
    margin: 0;
  }
  .log {
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    min-height: 44px;
  }
  .bar .btn-plain {
    margin-right: -8px;
    font-weight: 600;
  }

  .filters {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 12px;
    padding-top: 8px;
  }
  .filters .list-group {
    align-self: stretch;
  }
  .filters .list-row > span:first-child {
    flex: none;
  }
  .filters .list-row select,
  .filters .list-row input.value {
    flex: 1;
    min-width: 0;
    width: auto;
    padding: 0;
    background-color: transparent;
    color: var(--text-2);
    text-align: right;
    text-align-last: right;
  }
  .filters .list-row select {
    padding-right: 22px;
    background-position: right 0 center;
  }
  .unit {
    color: var(--text-3);
  }
  /* The whole row opens the term picker; its "Add" where a value would sit. */
  .adder {
    position: relative;
  }
  .adder .add {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 4px;
    padding: 0 var(--gutter);
    border: none;
    background: none;
    color: var(--text-2);
    font: inherit;
    cursor: pointer;
  }
  .adder .add:focus-visible {
    outline-offset: -2px;
  }
  .filters .picked {
    flex-wrap: wrap;
    gap: 8px;
    padding-top: 0;
    padding-bottom: 12px;
    box-shadow: none;
  }
  .clear {
    margin-left: 8px;
  }

  .queue {
    display: flex;
    flex-direction: column;
    gap: 20px;
    padding-top: 8px;
  }
  .queue p {
    margin: 0;
  }
  .arm {
    text-align: center;
  }
  .round-end {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 8px;
    padding-top: 24px;
    text-align: center;
  }
  .round-actions {
    align-self: stretch;
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin-top: 24px;
  }
  .round-actions button {
    width: 100%;
    min-height: 50px;
  }
  .placed ul {
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .spot {
    flex-direction: column;
    align-items: flex-start;
    gap: 2px;
  }
</style>
