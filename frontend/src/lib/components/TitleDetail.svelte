<script>
  // A sheet (decision 527) that leads with what a member opens it for; the rest of §6.0's card
  // (credits, scores, both DNA tiers) sits behind one disclosure. Model numbers arrive only with
  // Show the model on.
  import { get, post } from '$lib/api.js';
  import { facetColour, modelGate } from '$lib/home.svelte.js';
  import { runtimeLabel } from '$lib/rate.svelte.js';
  import { session } from '$lib/session.svelte.js';
  import { termLabel } from '$lib/terms.js';
  import {
    ANSWERS,
    CREDIT_TOP,
    answeredLine,
    creditJobs,
    creditKey,
    directedBy,
    displayNames,
    extractedByTerm,
    genreLine,
    placedBy,
    playWhy,
    projectedForCard,
    quoteText,
    revealLine,
    scoreLabel,
    sourceLabel,
    syncNote as syncNoteFor
  } from '$lib/titleCard.js';
  import RatePoster from '$lib/components/RatePoster.svelte';
  import Sheet from '$lib/components/Sheet.svelte';

  let { titleId, onClose, onPerson, onStateChange } = $props();

  let open = $state(true);
  let data = $state(null);
  // `error` is the load failing and replaces the card; an action failing must not.
  let error = $state('');
  let syncNote = $state('');
  let saving = $state(false);
  let answerNote = $state('');
  let answering = $state(false);
  // The server already omits the numbers when off; this gates only labels beside data always sent.
  const showModel = $derived(!!session.user?.show_model);
  const CREDIT_FOLD = 12;
  let showAllCredits = $state(false);
  // The quoted term whose evidence is shown; the first one until the member picks another.
  let picked = $state(null);
  // Run once the sheet has closed, so its history entry is gone before the caller moves on.
  let afterClose = null;

  // Scores arrive at full float precision: one decimal, trailing zero dropped.
  const round1 = (n) => (n === null || n === undefined ? '' : Number(Number(n).toFixed(1)));

  // Re-fetch on a new title, and when Show the model settles (the epoch, not the optimistic flag):
  // the model line is absent from the payload rather than hidden in it.
  $effect(() => {
    const id = titleId;
    void modelGate.epoch;
    let cancelled = false;
    data = null;
    error = '';
    syncNote = '';
    answerNote = '';
    // Reset too, or the previous film's credit count shows against this one's people.
    showAllCredits = false;
    picked = null;
    get(`/titles/${id}`)
      .then((res) => {
        if (!cancelled) data = res;
      })
      .catch((err) => {
        if (!cancelled) error = err.message;
      });
    return () => {
      cancelled = true;
    };
  });

  function closed() {
    open = false;
    const run = afterClose;
    afterClose = null;
    onClose?.();
    run?.();
  }

  // The app-side write never depends on Jellyfin (§7.3); the note says whether it was told. Busy,
  // not disabled, while it saves: a disabled button drops focus out of the sheet, and Escape with it.
  async function toggleSeen() {
    if (!data || saving) return;
    const next = data.title.seen_state === 'seen' ? 'unseen' : 'seen';
    saving = true;
    syncNote = '';
    try {
      const res = await post(`/titles/${data.title.id}/state`, { state: next });
      // The why line is absent for a seen title (decision 515), so drop it here too.
      data = {
        ...data,
        title: { ...data.title, seen_state: next },
        why: next === 'seen' ? null : data.why
      };
      syncNote = syncNoteFor(res);
      onStateChange?.(data.title.id, next);
    } catch (err) {
      syncNote = `Could not save that — ${err.message}`;
    } finally {
      saving = false;
    }
  }

  // Answered through §6.1's own session, so Undo, the counter and the reveal all apply (decision 487).
  async function answer(choice) {
    if (!data || answering) return;
    // A tap on the standing answer writes nothing: posted, it would be a fresh verdict row.
    const standing =
      choice === 'not_seen'
        ? data.title.seen_state !== 'seen'
        : data.title.seen_state === 'seen' && data.my_verdict?.label === choice;
    if (standing) return;
    answering = true;
    answerNote = '';
    try {
      const res = await post(`/rate/title/${data.title.id}`, { answer: choice });
      const next = choice === 'not_seen' ? 'unseen' : 'seen';
      data = {
        ...data,
        title: { ...data.title, seen_state: next },
        // Not seen writes no observation, so the verdict survives the flip (§4.2).
        my_verdict:
          choice === 'not_seen'
            ? data.my_verdict
            : { value: ['disliked', 'fine', 'liked'].indexOf(choice), label: choice },
        // Rated now, so the why line goes, as on the next open.
        why: choice === 'not_seen' ? data.why : null
      };
      answerNote = [answeredLine(choice), revealLine(res?.reveal)].filter(Boolean).join(' ');
      onStateChange?.(data.title.id, next);
    } catch (err) {
      answerNote = `Could not save that — ${err.message}`;
    } finally {
      answering = false;
    }
  }

  const runtime = $derived(runtimeLabel(data?.title));
  // `credits_for` returns every row; the disclosure spends what the payload holds.
  const shownCredits = $derived(
    showAllCredits ? (data?.credits ?? []) : (data?.credits ?? []).slice(0, CREDIT_FOLD)
  );
  // One list read in two places, so the count line counts every row shown.
  const topCredits = $derived(shownCredits.slice(0, CREDIT_TOP));
  const moreCredits = $derived(shownCredits.slice(CREDIT_TOP));
  const names = $derived(displayNames(data?.title));
  const directed = $derived(directedBy(data?.credits));
  // The server's reason for this member, or nothing: a card opened from search has none.
  const why = $derived(typeof data?.why === 'string' ? data.why.trim() : '');
  // Two tiers, two lists (§4.1 rule 1); nothing leaves the payload.
  const quoted = $derived(extractedByTerm(data?.dna?.extracted));
  const inferred = $derived(projectedForCard(data?.dna?.projected, data?.dna?.extracted));
  const evidence = $derived(quoted.find((tag) => tag.key === picked) ?? quoted[0] ?? null);
  const kindNoun = $derived(data?.title?.kind === 'series' ? 'series' : 'film');
  const scores = $derived(data?.platform_ratings?.items ?? []);
  // Joined in JS: Svelte collapses the whitespace around {#if} blocks.
  const subline = $derived(data ? [data.title.year ?? '—', runtime].filter(Boolean).join(' · ') : '');
  const pressed = (a) =>
    a.answer !== 'not_seen' &&
    data?.title.seen_state === 'seen' &&
    data?.my_verdict?.label === a.answer;
</script>

{#snippet icon(name, size = 20)}
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width={name === 'close' ? 2.25 : 1.75} stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    {#if name === 'liked'}
      <path d="M7 10.5V20H4.5a1 1 0 0 1-1-1v-7.5a1 1 0 0 1 1-1z" /><path d="M7 10.5 10.8 3.6a1.9 1.9 0 0 1 3.5 1.3L13.4 9h5.2a2 2 0 0 1 2 2.4l-1.4 7A2 2 0 0 1 17.2 20H7" />
    {:else if name === 'disliked'}
      <g transform="rotate(180 12 12)"><path d="M7 10.5V20H4.5a1 1 0 0 1-1-1v-7.5a1 1 0 0 1 1-1z" /><path d="M7 10.5 10.8 3.6a1.9 1.9 0 0 1 3.5 1.3L13.4 9h5.2a2 2 0 0 1 2 2.4l-1.4 7A2 2 0 0 1 17.2 20H7" /></g>
    {:else if name === 'fine'}
      <circle cx="12" cy="12" r="8.5" /><path d="M8.5 14.5h7" /><path d="M9.2 9.8h.01M14.8 9.8h.01" />
    {:else if name === 'not_seen'}
      <path d="M3.5 3.5l17 17" /><path d="M10.6 5.1A9.6 9.6 0 0 1 12 5c5 0 8.5 4.5 9.5 7a13 13 0 0 1-2.7 3.9" /><path d="M6.6 6.6C4.6 7.9 3.2 9.9 2.5 12c1 2.5 4.5 7 9.5 7 1.7 0 3.2-.5 4.6-1.2" /><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2" />
    {:else if name === 'play'}
      <path d="M8 5.5v13l10.5-6.5z" fill="currentColor" stroke="none" />
    {:else if name === 'trailer'}
      <circle cx="12" cy="12" r="8.5" /><path d="M10 8.8v6.4l5-3.2z" fill="currentColor" stroke="none" />
    {:else if name === 'check'}
      <path d="m5 12.5 4.5 4.5L19 7.5" />
    {:else if name === 'close'}
      <path d="M6 6l12 12M18 6 6 18" />
    {:else if name === 'chevron'}
      <path d="m9.5 5.5 6.5 6.5-6.5 6.5" />
    {/if}
  </svg>
{/snippet}

<!-- Callers key rows by `creditKey` (person and role class), so `onPerson` stays attached. -->
{#snippet person(c, close)}
  <button
    class="person"
    onclick={() => {
      afterClose = () => onPerson(c);
      close();
    }}
  >
    <span class="initial" aria-hidden="true">{c.name.charAt(0)}</span>
    <span class="who">
      <span class="pname">{c.name}</span>
      <span class="job"
        >{[creditJobs(c), showModel && c.sources?.length > 1 ? `${c.sources.length} sources` : null]
          .filter(Boolean)
          .join(' · ')}</span
      >
    </span>
  </button>
{/snippet}

<!-- Our read: outlined, apart from the quoted tier. A one-source chip is fainter, never absent. -->
{#snippet chip(p)}
  {@const n = p.weight == null ? null : Math.round(p.weight)}
  <span
    class="chip ourread"
    class:faint={n != null && n <= 1}
    title={[p.gloss, showModel && n != null ? `suggested by ${n} source${n === 1 ? '' : 's'}` : null]
      .filter(Boolean)
      .join(' - ') || undefined}
    data-weight={n}
  >
    <span class="dot" style:background={facetColour(p.facet)}></span>
    <span class="chiplabel">{termLabel(p)}</span>
    {#if showModel && n != null}
      <span class="n" aria-label={`${n} source${n === 1 ? '' : 's'}`}>{n}</span>
    {/if}
    {#if showModel}<span class="rawid">{p.term}</span>{/if}
  </span>
{/snippet}

<Sheet {open} onClose={closed} label="Title detail" width={880}>
  {#snippet children(close)}
    <button class="close" onclick={close} aria-label="Close">
      <span class="disc">{@render icon('close', 14)}</span>
    </button>

    {#if error}
      <p class="why err">{error}</p>
    {:else if !data}
      <p class="footnote loading">Loading…</p>
    {:else}
      {@const t = data.title}
      <div class="detail">
        <div class="lead">
          <div class="art"><RatePoster title={t} showName={false} /></div>

          <div class="head">
            <h2 class="title-1">{names.primary}</h2>
            {#if names.secondary}
              <p class="alt" data-testid="title-alt-name">{names.secondary}</p>
            {/if}
            <p class="sub">{subline}</p>
            {#if genreLine(data.genres)}
              <p class="alt" data-testid="title-genres">{genreLine(data.genres)}</p>
            {/if}
            {#if directed}<p class="footnote" data-testid="title-directed">{directed}</p>{/if}
          </div>

          <div class="main">
            {#if why}
              <p class="why" data-testid="title-why">{why}</p>
            {/if}

            {#if data.actions.play_on_jellyfin}
              <a class="btn-primary play" href={data.actions.play_on_jellyfin} target="_blank" rel="noreferrer">
                {@render icon('play')}Play on Jellyfin
              </a>
            {:else}
              <div class="playblock">
                <button class="btn-primary play" disabled>{@render icon('play')}Play on Jellyfin</button>
                <!-- A visible line, not a title= tooltip: touch has no hover. -->
                <p class="footnote" data-testid="title-jellyfin-why">
                  {playWhy(data.actions.play_reason ?? 'no_server')}
                </p>
              </div>
            {/if}

            <div class="answerblock">
              <h3 class="list-header">Your answer</h3>
              <div class="tiles" role="group" aria-label="Your answer" data-testid="title-rate">
                {#each ANSWERS as a (a.answer)}
                  <button
                    class="tile"
                    aria-pressed={pressed(a)}
                    aria-busy={answering}
                    data-answer={a.answer}
                    onclick={() => answer(a.answer)}
                  >
                    {@render icon(a.answer)}<span>{a.label}</span>
                  </button>
                {/each}
              </div>
              {#if answerNote}
                <p class="footnote" role="status" data-testid="title-rate-note">{answerNote}</p>
              {:else if !data.my_verdict}
                <p class="footnote">You haven't rated this yet.</p>
              {/if}
            </div>

            <div class="actions" class:pair={t.trailer_key}>
              {#if t.trailer_key}
                <a
                  class="btn-secondary trailer"
                  href={`https://www.youtube.com/watch?v=${t.trailer_key}`}
                  target="_blank"
                  rel="noreferrer"
                  aria-label="Watch the trailer on YouTube"
                >
                  {@render icon('trailer')}Trailer
                </a>
              {/if}
              <!-- Two states only (§4.2); this explicit action outranks what Jellyfin inferred (§7.3). -->
              <button
                class="btn-secondary seen"
                aria-pressed={t.seen_state === 'seen'}
                onclick={toggleSeen}
                aria-busy={saving}
                data-seen={t.seen_state ?? 'unseen'}
              >
                {@render icon('check')}{t.seen_state === 'seen' ? 'Watched' : 'Mark as watched'}
              </button>
              {#if data.actions.show_on_map}
                <!-- The server sends the target only once the Map ships. -->
                <a class="btn-secondary" href="/map?title={t.id}">Show on map</a>
              {/if}
            </div>
            {#if syncNote}
              <p class="footnote syncnote" role="status">{syncNote}</p>
            {/if}
            {#if t.kind === 'series' && t.seen_state === 'seen'}
              <!-- Un-marking a series would need a recursive DELETE over every episode, so it stays app-only. -->
              <p class="footnote" data-testid="title-series-unseen-note">
                Marking a series not seen is kept in Spielplan only — Jellyfin is never told to
                un-play its episodes.
              </p>
            {/if}

            {#if t.overview}<p class="overview">{t.overview}</p>{/if}

            {#if data.model_line}
              <!-- The server's own `text`, so the card and the rail print the same number. -->
              <div class="modelline" data-testid="title-model-line">
                {#if data.model_line.available}
                  <span class="data-lg">{data.model_line.text}</span>
                  {#if data.model_line.second_line}
                    <span class="data">{data.model_line.second_line}</span>
                  {/if}
                  {#if placedBy(data.model_line.e_source)}
                    <span class="footnote">{placedBy(data.model_line.e_source)}</span>
                  {/if}
                {:else}
                  <span class="footnote">No numbers for this one — {data.model_line.reason}</span>
                {/if}
              </div>
            {/if}
          </div>
        </div>

        {#if topCredits.length}
          <section class="cast">
            <h3 class="section-title">Cast &amp; crew</h3>
            <ul class="people strip" data-nobar>
              {#each topCredits as c (creditKey(c))}<li>{@render person(c, close)}</li>{/each}
            </ul>
          </section>
        {/if}

        <!-- A native <details>: the content stays in the document, and the browser owns the state. -->
        <details class="more" data-testid="title-more">
          <summary data-testid="title-more-toggle">
            <span>More about this {kindNoun}</span>{@render icon('chevron', 16)}
          </summary>

          <div class="morebody">
            <!-- Two tiers, visibly distinct and never interleaved (§4.1 rule 1). -->
            <section>
              <div class="heading">
                <h3 class="section-title">What it's like</h3>
                <p class="footnote">From reviews</p>
              </div>
              {#if quoted.length}
                <div class="chips" role="group" aria-label="What it's like">
                  {#each quoted as tag (tag.key)}
                    <button
                      class="pill tag"
                      aria-pressed={evidence?.key === tag.key}
                      title={tag.gloss ?? undefined}
                      onclick={() => (picked = tag.key)}
                    >
                      <span class="dot" style:background={facetColour(tag.facet)}></span>
                      <span class="term">{termLabel(tag)}</span>
                    </button>
                  {/each}
                </div>
                {#if evidence}
                  <div class="quotecard" data-testid="title-evidence">
                    <p class="quoted">
                      <span class="dot" style:background={facetColour(evidence.facet)}></span>
                      {termLabel(evidence)}
                      {#if showModel}
                        <span class="data">{[
                          evidence.term,
                          ...evidence.rows.map((r) => (r.salience != null ? `sal ${r.salience}` : null))
                        ]
                          .filter(Boolean)
                          .join(' · ')}</span>
                      {/if}
                    </p>
                    {#each evidence.evidence as e}
                      <blockquote class="quote">“{quoteText(e.quote)}”</blockquote>
                      <p class="footnote src">{showModel ? e.source : sourceLabel(e.source)}</p>
                    {/each}
                  </div>
                {/if}
              {:else}
                <p class="footnote">Nothing quoted from reviews yet.</p>
              {/if}

              {#if inferred.strong.length || inferred.weak.length}
                <div class="ourreadblock">
                  <p class="footnote">Our read · less certain</p>
                  {#if inferred.strong.length}
                    <div class="chips">
                      {#each inferred.strong as p (p.facet + ':' + p.term)}{@render chip(p)}{/each}
                    </div>
                  {/if}
                  {#if inferred.weak.length}
                    <!-- Folded, not dropped: a weight is never a filter (§4.1 rule 2). -->
                    <details class="weak" data-testid="title-weak-chips">
                      <summary>Show {inferred.weak.length} more</summary>
                      <div class="chips">
                        {#each inferred.weak as p (p.facet + ':' + p.term)}{@render chip(p)}{/each}
                      </div>
                    </details>
                  {/if}
                </div>
              {/if}
            </section>

            {#if scores.length}
              <section>
                <div class="heading">
                  <h3 class="section-title">Scores elsewhere</h3>
                  <p class="footnote">{data.platform_ratings.note}</p>
                </div>
                <div class="scores">
                  <!-- Keyed by platform and metric: one platform ships two scores on different scales. -->
                  {#each scores as p (p.platform + ':' + p.metric)}
                    <div class="score">
                      <!-- The scale travels with the number: this block mixes 10- and 100-point scales. -->
                      <span class="value">{round1(p.score)}<span class="of">/{round1(p.scale)}</span></span>
                      <span class="footnote">{scoreLabel(p, scores)}</span>
                    </div>
                  {/each}
                </div>
              </section>
            {/if}

            {#if moreCredits.length}
              <section>
                <div class="heading">
                  <h3 class="section-title">More cast &amp; crew</h3>
                  <p class="footnote" data-testid="credit-count"
                    >{shownCredits.length.toLocaleString()} of {data.credits.length.toLocaleString()}</p
                  >
                </div>
                <ul class="people list">
                  {#each moreCredits as c (creditKey(c))}<li>{@render person(c, close)}</li>{/each}
                </ul>
                {#if data.credits.length > CREDIT_FOLD}
                  <!-- Both labels use the constant, so raising CREDIT_FOLD cannot leave a stale word. -->
                  <button
                    class="btn-plain disclose"
                    data-testid="credits-disclosure"
                    aria-expanded={showAllCredits}
                    onclick={() => (showAllCredits = !showAllCredits)}
                  >
                    {showAllCredits
                      ? `Show ${CREDIT_FOLD}`
                      : `Show all ${data.credits.length.toLocaleString()}`}
                  </button>
                {/if}
              </section>
            {/if}
          </div>
        </details>
      </div>
    {/if}
  {/snippet}
</Sheet>

<style>
  p,
  h2,
  h3,
  ul,
  blockquote {
    margin: 0;
  }
  /* Over the sheet's corner, outside its scroller; the 48px button draws a 30px disc. */
  .close {
    position: absolute;
    top: 4px;
    right: 4px;
    z-index: 3;
    width: var(--touch);
    height: var(--touch);
    padding: 0;
    border: none;
    background: none;
    display: grid;
    place-items: center;
  }
  .disc {
    width: 30px;
    height: 30px;
    border-radius: var(--r-pill);
    background: rgba(245, 240, 232, 0.12);
    color: var(--text-2);
    display: grid;
    place-items: center;
  }
  .loading,
  .err {
    padding: 24px 0;
  }
  .err {
    color: var(--negative);
  }

  .detail {
    display: flex;
    flex-direction: column;
    gap: 32px;
    padding-top: 8px;
  }
  .lead {
    display: grid;
    grid-template-columns: 120px minmax(0, 1fr);
    grid-template-areas: 'art head' 'main main';
    gap: 16px;
    align-items: end;
  }
  .art {
    grid-area: art;
  }
  .head {
    grid-area: head;
    display: flex;
    flex-direction: column;
    gap: 4px;
    /* Clears the close button beside the name. */
    padding-right: 24px;
  }
  .main {
    grid-area: main;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  .alt,
  .sub {
    font-size: var(--fs-subhead);
    line-height: 20px;
    color: var(--text-2);
  }
  .sub {
    font-variant-numeric: tabular-nums;
  }
  .play {
    width: 100%;
    min-height: 50px;
    border-radius: var(--r-md);
    text-decoration: none;
  }
  .playblock,
  .answerblock {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .answerblock {
    padding-top: 8px;
  }
  .answerblock .list-header {
    padding: 0;
  }
  /* Four equal tiles, worst to best, and Not seen a step apart (decision 527). */
  .tiles {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr)) 0 minmax(0, 1fr);
    gap: 8px;
  }
  .tile[data-answer='not_seen'] {
    grid-column: 5;
  }
  .tile {
    min-height: 60px;
    border: none;
    border-radius: var(--r-md);
    background: var(--surface-1);
    color: var(--text);
    padding: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 4px;
    font-size: var(--fs-footnote);
    line-height: 18px;
    font-weight: 600;
    transition: background 0.12s var(--ease);
  }
  .tile:hover {
    background: var(--surface-2);
  }
  .tile[aria-pressed='true'],
  .tile[aria-pressed='true']:hover {
    background: var(--text);
    color: var(--bg);
  }
  .actions {
    display: grid;
    gap: 12px;
  }
  .actions.pair {
    grid-template-columns: minmax(0, 2fr) minmax(0, 3fr);
  }
  .actions > * {
    white-space: nowrap;
    border-radius: var(--r-md);
    text-decoration: none;
  }
  .actions .btn-secondary:not(.trailer):not(.seen) {
    grid-column: 1 / -1;
  }
  .overview {
    font-size: var(--fs-body);
    line-height: 22px;
    color: var(--text-2);
    text-wrap: pretty;
  }
  .modelline {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 12px;
    border-radius: var(--r-sm);
    background: var(--surface-1);
  }

  .cast {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  ul.people {
    list-style: none;
  }
  .strip {
    display: flex;
    gap: 4px;
    overflow-x: auto;
    margin: 0 calc(-1 * var(--gutter));
    padding: 0 var(--gutter);
  }
  .person {
    border: none;
    background: none;
    color: inherit;
    padding: 0;
    cursor: pointer;
    text-align: left;
  }
  .initial {
    flex: none;
    border-radius: var(--r-pill);
    background: var(--surface-2);
    color: var(--text-2);
    display: grid;
    place-items: center;
    font-weight: 600;
  }
  .who {
    display: flex;
    flex-direction: column;
    min-width: 0;
  }
  .pname {
    font-size: var(--fs-footnote);
    line-height: 18px;
  }
  .job {
    font-size: var(--fs-caption);
    line-height: 16px;
    font-weight: 500;
    color: var(--text-3);
  }
  .strip .person {
    width: 84px;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 8px;
    text-align: center;
  }
  .strip .initial {
    width: 56px;
    height: 56px;
    font-size: var(--fs-section);
    line-height: 25px;
  }
  .list {
    padding: 0;
    border-radius: var(--r-md);
    background: var(--surface-1);
    overflow: hidden;
  }
  .list li + li {
    box-shadow: inset 0 0.5px 0 var(--separator);
  }
  .list .person {
    width: 100%;
    min-height: 52px;
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 8px var(--gutter);
  }
  .list .initial {
    width: 30px;
    height: 30px;
    font-size: var(--fs-footnote);
  }
  .list .pname {
    font-size: var(--fs-body);
    line-height: 22px;
  }
  .list .job {
    font-size: var(--fs-footnote);
    line-height: 18px;
    font-weight: 400;
  }

  .more > summary {
    display: flex;
    align-items: center;
    gap: 12px;
    min-height: 52px;
    padding: 0 12px 0 var(--gutter);
    border-radius: var(--r-md);
    background: var(--surface-1);
    font-size: var(--fs-body);
    line-height: 22px;
    cursor: pointer;
    list-style: none;
  }
  .more > summary > span {
    flex: 1;
  }
  .more > summary > :global(svg) {
    color: rgba(245, 240, 232, 0.35);
    transition: transform 0.2s var(--ease);
  }
  .more[open] > summary > :global(svg) {
    transform: rotate(90deg);
  }
  .more > summary::-webkit-details-marker,
  .weak > summary::-webkit-details-marker {
    display: none;
  }
  .morebody {
    display: flex;
    flex-direction: column;
    gap: 32px;
    padding-top: 24px;
  }
  .morebody > section {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .heading {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .dot {
    width: 6px;
    height: 6px;
    flex: none;
    border-radius: var(--r-pill);
  }
  .tag {
    min-height: 30px;
    padding: 0 12px;
    font-weight: 400;
  }
  .tag[aria-pressed='true'] {
    font-weight: 600;
  }
  .quotecard {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: var(--card-pad);
    border-radius: var(--r-md);
    background: var(--surface-1);
  }
  .quoted {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 6px;
    font-size: var(--fs-footnote);
    line-height: 18px;
    font-weight: 600;
    color: var(--text-2);
  }
  .quoted .data {
    font-weight: 400;
  }
  .quote {
    font-size: var(--fs-body);
    line-height: 22px;
    font-style: italic;
  }
  .ourreadblock {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding-top: 4px;
  }
  .ourread {
    min-height: 30px;
    padding: 0 12px;
    background: none;
    box-shadow: inset 0 0 0 1px rgba(255, 240, 225, 0.16);
    color: var(--text-2);
    font-weight: 400;
    cursor: default;
  }
  /* One source behind it: fainter, never absent (§4.1 rule 2). */
  .ourread.faint {
    opacity: 0.6;
  }
  .n,
  .rawid {
    color: var(--text-3);
    font-size: var(--fs-footnote);
    font-variant-numeric: tabular-nums;
  }
  .weak > summary {
    display: inline-flex;
    align-items: center;
    min-height: 44px;
    color: var(--accent-text);
    font-size: var(--fs-subhead);
    cursor: pointer;
    list-style: none;
  }
  .weak[open] > summary {
    display: none;
  }
  .scores {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
  }
  .score {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: var(--card-pad);
    border-radius: var(--r-md);
    background: var(--surface-1);
  }
  .value {
    font-size: var(--fs-section);
    line-height: 25px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }
  .of {
    font-size: var(--fs-footnote);
    font-weight: 400;
    color: var(--text-3);
  }
  .disclose {
    align-self: flex-start;
    padding: 0;
  }

  /* A desktop sheet is a centred panel: the poster beside everything the phone stacks under it. */
  @media (min-width: 721px) {
    .detail {
      padding-top: 16px;
    }
    .lead {
      grid-template-columns: 280px minmax(0, 1fr);
      grid-template-areas: 'art head' 'art main';
      grid-template-rows: auto 1fr;
      column-gap: 32px;
      row-gap: 16px;
      align-items: start;
    }
    .head {
      padding-right: 40px;
    }
    .head .title-1 {
      font-size: var(--fs-display);
      line-height: 48px;
    }
    .play {
      width: auto;
      align-self: flex-start;
      padding: 0 48px;
    }
    .actions,
    .actions.pair {
      display: flex;
      flex-wrap: wrap;
    }
    .scores {
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }
    .strip {
      margin: 0;
      padding: 0;
      gap: 16px;
    }
  }
</style>
