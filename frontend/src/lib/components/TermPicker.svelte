<script>
  // What it's like (decision 557, boards B2 and B3): the vocabulary, browsed by facet while the
  // field is empty and ranked as it is typed, each term to include or leave out. A sheet on a
  // phone; on a desktop a popover under the Filters cell that opened it.
  import { countLabel, facetColour } from '$lib/home.svelte.js';
  import { browseFacets, facetName, loadVocabulary, rankTerms } from '$lib/filters.svelte.js';
  import { session } from '$lib/session.svelte.js';
  import FilterChip from './FilterChip.svelte';
  import Icon from './Icon.svelte';
  import Popover from './Popover.svelte';
  import Sheet from './Sheet.svelte';

  let {
    open = false,
    kinds = ['movie'],
    chosen = [],
    onInclude,
    onLeaveOut,
    onRemove,
    onClose,
    anchor = null,
    onSearchTitles = null
  } = $props();

  // Each facet's first rows on a phone; a desktop pane shows browseFacets' eight.
  const PHONE_TOP = 3;

  let width = $state(typeof window === 'undefined' ? 390 : window.innerWidth);
  const desktop = $derived(Boolean(anchor) && width > 720);

  let vocab = $state(null);
  let error = $state('');
  let q = $state('');
  // The facet the desktop pane shows, and the one listed whole after "All N in ...".
  let pane = $state('');
  let whole = $state('');
  let field = $state();
  /** @type {Record<string, HTMLElement>} */
  const sections = $state({});

  const kindsKey = $derived(kinds.join(','));
  const noun = $derived(kinds.length > 1 ? 'titles' : kinds[0] === 'series' ? 'series' : 'films');
  const facets = $derived(vocab ? browseFacets(vocab) : []);
  const current = $derived(facets.find((f) => f.facet === pane) ?? facets[0]);
  const query = $derived(q.trim());
  const hits = $derived(vocab && query ? rankTerms(vocab, query) : []);
  const showModel = $derived(!!session.user?.show_model);
  const chips = $derived([...chosen.filter((t) => t.mode === 'in'), ...chosen.filter((t) => t.mode === 'out')]);

  let reads = 0;
  $effect(() => {
    if (!open) return;
    const seq = ++reads;
    q = '';
    whole = '';
    error = '';
    loadVocabulary(kindsKey.split(',')).then(
      (read) => {
        if (seq === reads) vocab = read;
      },
      (err) => {
        if (seq === reads) error = err.message;
      }
    );
  });

  // A popover is not a dialog that takes focus; the field it opens on does.
  $effect(() => {
    if (open && desktop && field) field.focus({ preventScroll: true });
  });

  const asTerm = (t) => ({ term: t.term ?? t.id, label: t.label, facet: t.facet });
  const modeOf = (id) => chosen.find((c) => c.id === id)?.mode ?? null;

  // A pressed Include or Leave out takes the term back off.
  function pick(t, mode) {
    const term = asTerm(t);
    if (modeOf(term.term) === mode) onRemove?.(term);
    else (mode === 'in' ? onInclude : onLeaveOut)?.(term);
  }

  function hitMeta(t) {
    const parts = [facetName(t.facet), t.via ? `via ${t.via}` : null];
    return [...parts, desktop ? null : countLabel({ total: t.owned, kinds })].filter(Boolean).join(' · ');
  }
</script>

<svelte:window bind:innerWidth={width} />

{#snippet row(t, meta)}
  {@const mode = modeOf(t.term)}
  <div class="row" data-testid="term-row">
    <span class="dot" style:background={facetColour(t.facet)} aria-hidden="true"></span>
    <span class="text">
      <span class="label">{t.label}{#if showModel}<span class="rawid">{t.term}</span>{/if}</span>
      {#if meta}<span class="meta">{meta}</span>{/if}
    </span>
    {#if desktop}<span class="n">{t.owned.toLocaleString()}</span>{/if}
    <span class="acts">
      <button class="act press" aria-pressed={mode === 'in'} aria-label="Include {t.label}" onclick={() => pick(t, 'in')}>Include</button>
      <button class="act press" aria-pressed={mode === 'out'} aria-label="Leave out {t.label}" onclick={() => pick(t, 'out')}>Leave out</button>
    </span>
  </div>
{/snippet}

{#snippet all(f)}
  <button class="all" onclick={() => (whole = f.facet)}>
    <span>All {f.total} in {f.name}</span><Icon name="chevron-right" size={16} />
  </button>
{/snippet}

{#snippet colhead(left)}
  <div class="colhead" aria-hidden="true">
    <span class="grow">{left}</span><span>In your library</span>
    <span class="acts ghost"><span class="act">Include</span><span class="act">Leave out</span></span>
  </div>
{/snippet}

{#snippet top()}
  <label class="find">
    <Icon name="search" size={18} />
    <input
      type="search"
      bind:this={field}
      bind:value={q}
      autocomplete="off"
      enterkeyhint="search"
      aria-label="Find a taste term"
      placeholder="Search taste terms"
      data-testid="term-search"
    />
  </label>
  {#if chips.length}
    <div class="chosen" role="group" aria-label="Chosen terms">
      {#each chips as t (t.id)}
        <FilterChip
          variant="term"
          mode={t.mode}
          label={t.label}
          facet={t.facet}
          testid="picker-term-chip"
          onFlip={() => (t.mode === 'in' ? onLeaveOut : onInclude)?.(asTerm(t))}
          onRemove={() => onRemove?.(asTerm(t))}
        />
      {/each}
    </div>
  {/if}
  {#if !desktop && vocab && !query && facets.length}
    <nav class="jump" aria-label="Kinds of taste term" data-nobar>
      {#each facets as f (f.facet)}
        <button class="pill" onclick={() => sections[f.facet]?.scrollIntoView?.({ block: 'start' })}>
          <span class="dot big" style:background={f.colour} aria-hidden="true"></span>{f.name}
        </button>
      {/each}
    </nav>
  {/if}
{/snippet}

{#snippet list()}
  {#if error}
    <p class="footnote note" role="alert">{error}</p>
  {:else if !vocab}
    <p class="footnote note">Loading the taste terms…</p>
  {:else if query}
    {#if hits.length}
      {#if desktop}
        {@render colhead('Taste terms')}
        {#each hits as t (t.term)}{@render row(t, hitMeta(t))}{/each}
      {:else}
        <p class="footnote note">Counts are {noun} in your library.</p>
        <div class="list-group">
          {#each hits as t (t.term)}{@render row(t, hitMeta(t))}{/each}
        </div>
      {/if}
    {:else}
      <div class="none" data-testid="term-none">
        <p>No taste term matches {query}</p>
        {#if onSearchTitles}
          <button class="btn-plain" onclick={() => onSearchTitles(query)}>Search titles for {query}</button>
        {/if}
      </div>
    {/if}
  {:else if !facets.length}
    <p class="footnote note">No taste terms yet.</p>
  {:else if desktop}
    <div class="panes">
      <div class="facets" role="group" aria-label="Kinds of taste term">
        {#each facets as f (f.facet)}
          <button
            class="facet"
            aria-pressed={f.facet === current.facet}
            onclick={() => {
              pane = f.facet;
              whole = '';
            }}
          >
            <span class="dot big" style:background={f.colour} aria-hidden="true"></span>
            <span class="name">{f.name}</span>
            <span class="preview">{f.top.slice(0, 3).map((t) => t.label).join(', ')}</span>
          </button>
        {/each}
      </div>
      <section class="pane" aria-label={current.name}>
        <div class="panehead">
          <span class="dot big" style:background={current.colour} aria-hidden="true"></span>
          <h3>{current.name}</h3>
          <span class="footnote">{current.total} terms</span>
        </div>
        {@render colhead('')}
        {#each whole === current.facet ? current.terms : current.top as t (t.term)}{@render row(t, '')}{/each}
        {#if whole !== current.facet && current.total > current.top.length}{@render all(current)}{/if}
      </section>
    </div>
  {:else}
    <p class="footnote note">Counts are {noun} in your library.</p>
    {#each facets as f (f.facet)}
      <section bind:this={sections[f.facet]} aria-label={f.name}>
        <h3 class="list-header facethead">
          <span class="dot big" style:background={f.colour} aria-hidden="true"></span>{f.name}
        </h3>
        <div class="list-group">
          {#each whole === f.facet ? f.terms : f.top.slice(0, PHONE_TOP) as t (t.term)}
            {@render row(t, countLabel({ total: t.owned, kinds }))}
          {/each}
          {#if whole !== f.facet && f.total > PHONE_TOP}{@render all(f)}{/if}
        </div>
      </section>
    {/each}
  {/if}
{/snippet}

{#if desktop}
  <Popover {open} {anchor} label="What it's like" width={720} {onClose}>
    <div class="picker desktop">
      <div class="top">{@render top()}</div>
      <div class="list">{@render list()}</div>
    </div>
  </Popover>
{:else}
  <Sheet {open} {onClose} label="What it's like">
    {#snippet header(close)}
      <div class="sheethead">
        <h2 class="section-title">What it's like</h2>
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
  .jump {
    display: flex;
    gap: 8px;
    margin: 0 calc(-1 * var(--gutter));
    padding: 0 var(--gutter);
    overflow-x: auto;
  }
  .jump .pill {
    flex: none;
    gap: 8px;
    font-weight: 500;
  }
  .note {
    margin: 0;
    padding: 4px 4px 8px;
  }
  .facethead {
    display: flex;
    align-items: center;
    gap: 8px;
    padding-top: 24px;
  }
  .dot {
    flex: none;
    width: 6px;
    height: 6px;
    border-radius: var(--r-pill);
  }
  .dot.big {
    width: 8px;
    height: 8px;
  }

  .row {
    display: flex;
    align-items: center;
    gap: 10px;
    min-height: 64px;
    padding: 10px 10px 10px 16px;
  }
  .row + .row {
    box-shadow: inset 0 0.5px 0 var(--separator);
  }
  .text {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .label {
    min-width: 0;
    overflow-wrap: anywhere;
    font-size: var(--fs-body);
    line-height: 22px;
  }
  .rawid {
    margin-left: 6px;
  }
  .meta,
  .rawid {
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
  /* 32 drawn, 48 to tap. */
  button.act::after {
    content: '';
    position: absolute;
    inset: -8px -3px;
  }
  .all {
    width: 100%;
    min-height: 48px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 16px;
    border: none;
    background: none;
    box-shadow: inset 0 0.5px 0 var(--separator);
    color: var(--accent-text);
    font-size: var(--fs-subhead);
    line-height: 20px;
    text-align: left;
  }
  .none {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 4px;
    padding: 16px 4px;
  }
  .none p {
    margin: 0;
    color: var(--text-2);
  }
  .none .btn-plain {
    margin-left: -8px;
    font-size: var(--fs-subhead);
  }

  /* The desktop popover: the field and chips stay, the list scrolls, browsing is two panes. */
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
    display: flex;
    flex-direction: column;
    overflow-y: auto;
    padding: 0 6px 6px;
  }
  .desktop .note {
    padding: 4px 8px 8px;
  }
  .desktop .row {
    min-height: 44px;
    padding: 0 8px 0 12px;
    border-radius: var(--r-sm);
  }
  .desktop .text {
    flex-direction: row;
    align-items: baseline;
    gap: 10px;
  }
  .desktop .label {
    flex: 0 1 auto;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: var(--fs-subhead);
    line-height: 20px;
  }
  .desktop .meta {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .n {
    flex: none;
    width: 40px;
    text-align: right;
    font-size: var(--fs-subhead);
    line-height: 20px;
    color: var(--text-2);
    font-variant-numeric: tabular-nums;
  }
  .colhead {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 6px 8px 4px 12px;
    font-size: var(--fs-footnote);
    line-height: 18px;
    letter-spacing: 0.02em;
    text-transform: uppercase;
    color: var(--text-3);
    white-space: nowrap;
  }
  .grow {
    flex: 1;
  }
  .ghost {
    visibility: hidden;
    letter-spacing: normal;
    text-transform: none;
  }
  .panes {
    flex: 1 1 440px;
    min-height: 0;
    display: grid;
    grid-template-columns: 240px minmax(0, 1fr);
    grid-template-rows: minmax(0, 1fr);
    margin: 0 -6px -6px;
    box-shadow: inset 0 0.5px 0 var(--separator);
  }
  .facets,
  .pane {
    min-height: 0;
    overflow-y: auto;
    overscroll-behavior: contain;
  }
  .facets {
    display: flex;
    flex-direction: column;
    padding: 8px;
    box-shadow: inset -0.5px 0 0 var(--separator);
  }
  .facet {
    flex: none;
    display: flex;
    align-items: center;
    gap: 10px;
    min-height: 40px;
    padding: 0 10px;
    border: none;
    border-radius: 8px;
    background: none;
    color: var(--text);
    text-align: left;
  }
  .facet[aria-pressed='true'] {
    background: var(--surface-2);
  }
  .facet .name {
    flex: none;
    font-size: var(--fs-subhead);
    line-height: 20px;
    font-weight: 500;
  }
  .facet[aria-pressed='true'] .name {
    font-weight: 600;
  }
  .preview {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-3);
  }
  .pane {
    padding: 0 8px 8px;
  }
  .panehead {
    display: flex;
    align-items: center;
    gap: 8px;
    min-height: 44px;
    padding: 8px 8px 0;
  }
  .panehead h3 {
    margin: 0;
    font-size: var(--fs-body);
    line-height: 22px;
    font-weight: 600;
  }
  .pane .all {
    padding: 0 8px;
    border-radius: 8px;
  }
</style>
