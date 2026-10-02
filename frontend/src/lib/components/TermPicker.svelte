<script>
  // What it's like (decision 557, boards B2, B3 and B8): the vocabulary, browsed by facet while the
  // field is empty and ranked as it is typed, each term to include or leave out. A sheet on a phone;
  // `inline` on a desktop, where the Filters cell is its field and the list hangs under it: a popover
  // on Home, rows under the field's own in Rank's Filters (`drop="below"`).
  import { dismiss } from '$lib/dismiss.js';
  import { countLabel, facetColour } from '$lib/home.svelte.js';
  import { browseFacets, facetName, loadVocabulary, rankTerms } from '$lib/filters.svelte.js';
  import { session } from '$lib/session.svelte.js';
  import { termLabel } from '$lib/terms.js';
  import FilterChip from './FilterChip.svelte';
  import Icon from './Icon.svelte';
  import Popover from './Popover.svelte';
  import Sheet from './Sheet.svelte';
  import TokenField from './TokenField.svelte';

  let {
    open = false,
    kinds = ['movie'],
    chosen = [],
    onInclude,
    onLeaveOut,
    onRemove,
    onClose = undefined,
    onSearchTitles = null,
    inline = false,
    drop = 'popover',
    testid = 'filter-terms',
    chipTestid = 'term-chip'
  } = $props();

  // Each facet's first rows in a list of sections; Home's desktop pane shows browseFacets' eight.
  const SECTION_TOP = 3;
  const listId = $props.id();

  let vocab = $state(null);
  let error = $state('');
  let q = $state('');
  // The inline field's list, opened by a press or by typing.
  let listed = $state(false);
  // The hit Enter takes.
  let active = $state(0);
  // The facet the desktop pane shows, and the one listed whole after "All N in ...".
  let pane = $state('');
  let whole = $state('');
  let field = $state();
  let input = $state();
  /** @type {Record<string, HTMLElement>} */
  const sections = $state({});

  const below = $derived(drop === 'below');
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
    if (!(inline ? listed : open)) return;
    const seq = ++reads;
    // A sheet opens on an empty field; the inline field keeps what was typed.
    if (!inline) {
      q = '';
      whole = '';
    }
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
    return [...parts, inline ? null : countLabel({ total: t.owned, kinds })].filter(Boolean).join(' · ');
  }

  // Arrows move the highlight, Enter includes it and Shift+Enter leaves it out (board B3).
  function onKey(event) {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      listed = true;
      active = Math.max(0, Math.min(hits.length - 1, active + (event.key === 'ArrowDown' ? 1 : -1)));
    } else if (event.key === 'Enter' && listed && hits[active]) {
      event.preventDefault();
      pick(hits[active], event.shiftKey ? 'out' : 'in');
    }
  }

  // Inside Rank's Filters, an Escape that shuts the list leaves the sheet open.
  function closeBelow(event) {
    if (!listed) return;
    listed = false;
    if (event?.type !== 'keydown') return;
    event.stopPropagation();
    input?.focus();
  }

  function searchTitles() {
    const words = query;
    q = '';
    listed = false;
    onSearchTitles?.(words);
  }
</script>

{#snippet row(t, meta, on)}
  {@const mode = modeOf(t.term)}
  <div class="row" class:active={on} data-testid="term-row">
    <span class="dot" style:background={facetColour(t.facet)} aria-hidden="true"></span>
    <span class="text">
      <span class="label">{t.label}{#if showModel}<span class="rawid">{t.term}</span>{/if}</span>
      {#if meta}<span class="meta">{meta}</span>{/if}
    </span>
    {#if inline}<span class="n">{t.owned.toLocaleString()}</span>{/if}
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
  {#if vocab && !query && facets.length}
    <nav class="jump" aria-label="Kinds of taste term" data-nobar>
      {#each facets as f (f.facet)}
        <button class="pill" onclick={() => sections[f.facet]?.scrollIntoView?.({ block: 'start' })}>
          <span class="dot big" style:background={f.colour} aria-hidden="true"></span>{f.name}
        </button>
      {/each}
    </nav>
  {/if}
{/snippet}

{#snippet fieldChips()}
  {#each chips as t (t.id)}
    <FilterChip
      variant="term"
      mode={t.mode}
      label={t.label || termLabel(t.id)}
      facet={t.facet}
      testid={chipTestid}
      onFlip={() => (t.mode === 'in' ? onLeaveOut : onInclude)?.(asTerm(t))}
      onRemove={() => onRemove?.(asTerm(t))}
    />
  {/each}
{/snippet}

{#snippet tokens()}
  <TokenField
    bind:value={q}
    bind:field
    bind:input
    label="Find a taste term"
    placeholder="Add a term"
    {testid}
    inputTestid="term-search"
    expanded={listed}
    controls={listId}
    onpress={() => (listed = true)}
    oninput={() => {
      listed = true;
      active = 0;
    }}
    onkeydown={onKey}
    chips={fieldChips}
  />
{/snippet}

{#snippet list()}
  {#if error}
    <p class="footnote note" role="alert">{error}</p>
  {:else if !vocab}
    <p class="footnote note">Loading the taste terms…</p>
  {:else if query}
    {#if hits.length}
      {#if inline}
        {@render colhead('Taste terms')}
        {#each hits as t, i (t.term)}{@render row(t, hitMeta(t), i === active)}{/each}
      {:else}
        <p class="footnote note">Counts are {noun} in your library.</p>
        <div class="list-group">
          {#each hits as t (t.term)}{@render row(t, hitMeta(t), false)}{/each}
        </div>
      {/if}
    {:else}
      <div class="none" data-testid="term-none">
        <p>No taste term matches {query}</p>
        {#if onSearchTitles}
          <button class="btn-plain" onclick={searchTitles}>Search titles for {query}</button>
        {/if}
      </div>
    {/if}
  {:else if !facets.length}
    <p class="footnote note">No taste terms yet.</p>
  {:else if inline && !below}
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
        {#each whole === current.facet ? current.terms : current.top as t (t.term)}{@render row(t, '', false)}{/each}
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
          {#each whole === f.facet ? f.terms : f.top.slice(0, SECTION_TOP) as t (t.term)}
            {@render row(t, inline ? '' : countLabel({ total: t.owned, kinds }), false)}
          {/each}
          {#if whole !== f.facet && f.total > SECTION_TOP}{@render all(f)}{/if}
        </div>
      </section>
    {/each}
  {/if}
{/snippet}

{#if !inline}
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
{:else if below}
  <div class="termbox" use:dismiss={closeBelow}>
    <div class="list-row fieldrow">
      <span>What it's like</span>
      {@render tokens()}
    </div>
    {#if listed}<div class="picker desktop below" id={listId}>{@render list()}</div>{/if}
  </div>
{:else}
  {@render tokens()}
  <Popover open={listed} anchor={field} label="What it's like" width={720} onClose={() => (listed = false)}>
    <div class="picker desktop" id={listId}>{@render list()}</div>
  </Popover>
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

  /* The desktop list: one scroller under the field; browsing on Home is two panes, each scrolling. */
  .picker.desktop {
    flex: 1 1 auto;
    min-height: 0;
    display: flex;
    flex-direction: column;
    overflow-y: auto;
    padding: 6px;
  }
  .desktop .note {
    padding: 4px 8px 8px;
  }
  .desktop .row {
    min-height: 44px;
    padding: 0 8px 0 12px;
    border-radius: var(--r-sm);
  }
  .desktop .row.active {
    background: var(--surface-2);
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
    flex: 1 1 auto;
    min-height: 0;
    display: grid;
    grid-template-columns: 240px minmax(0, 1fr);
    grid-template-rows: minmax(0, 1fr);
    margin: -6px;
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

  /* Rank's Filters (board B8): the field in its row, the list as rows under it in the same sheet. */
  .termbox {
    display: flex;
    flex-direction: column;
    box-shadow: inset 0 0.5px 0 var(--separator);
  }
  .fieldrow > span {
    flex: none;
  }
  .fieldrow > :global(.tokens) {
    flex: 1;
  }
  .picker.below {
    overflow: visible;
    padding: 0 8px 8px;
    box-shadow: inset 0 0.5px 0 var(--separator);
  }
  .below .row {
    min-height: 50px;
  }
  .below .text {
    flex-direction: column;
    align-items: stretch;
    gap: 0;
  }
  .below .facethead {
    padding: 16px 8px 6px;
  }
  .below .list-group {
    background: none;
  }
</style>
