<script>
  // Leads with what a member opens it for; the rest of §6.0's card (credits, scores, both DNA
  // tiers) sits behind one disclosure. Model numbers arrive only with Show the model on.
  import { get, post } from '$lib/api.js';
  import { facetColour, modelGate } from '$lib/home.svelte.js';
  import { KIND_LABELS, runtimeLabel } from '$lib/rate.svelte.js';
  import { session } from '$lib/session.svelte.js';
  import { termLabel } from '$lib/terms.js';
  import {
    ANSWERS,
    CREDIT_TOP,
    answeredLine,
    creditJobs,
    creditKey,
    displayNames,
    extractedByTerm,
    playWhy,
    projectedForCard,
    quoteText,
    revealLine,
    sourceLabel,
    syncNote as syncNoteFor
  } from '$lib/titleCard.js';
  import { dismiss } from '$lib/dismiss.js';
  import RatePoster from '$lib/components/RatePoster.svelte';

  let { titleId, onClose, onPerson, onStateChange } = $props();

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

  // Scores arrive at full float precision: one decimal, trailing zero dropped.
  const round1 = (n) => (n === null || n === undefined ? '' : Number(Number(n).toFixed(1)));
  // `user_score` / `critic_score` / `audience_rating_count` are the corpus's own metric names.
  const metricLabel = (m) => String(m ?? '').replace(/_/g, ' ');

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

  // The app-side write never depends on Jellyfin (§7.3); the note says whether it was told.
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
  // The server's reason for this member, or nothing: a card opened from search has none.
  const why = $derived(typeof data?.why === 'string' ? data.why.trim() : '');
  // Two tiers, two lists (§4.1 rule 1); nothing leaves the payload.
  const quoted = $derived(extractedByTerm(data?.dna?.extracted));
  const inferred = $derived(projectedForCard(data?.dna?.projected, data?.dna?.extracted));
  const kindNoun = $derived(data?.title?.kind === 'series' ? 'series' : 'film');
  // Joined in JS: Svelte collapses the whitespace around {#if} blocks.
  const subline = $derived(
    data
      ? [data.title.year ?? '—', runtime, KIND_LABELS[data.title.kind] ?? data.title.kind,
         data.title.seen_state === 'seen' ? 'seen' : null].filter(Boolean).join(' · ')
      : ''
  );
</script>

<!-- Callers key rows by `creditKey` (person and role class), so `onPerson` stays attached. -->
{#snippet creditRow(c)}
  <button class="person" onclick={() => onPerson(c)}>
    <span class="dot">{c.name.charAt(0)}</span>
    <span class="pname">{c.name}</span>
    <span class="data"
      >{[creditJobs(c), showModel && c.sources?.length > 1 ? `${c.sources.length} sources` : null]
        .filter(Boolean)
        .join(' · ')}</span
    >
  </button>
{/snippet}

<!-- The weight counts sources: a one-source chip is fainter, and the number is Show the model's. -->
{#snippet chip(p)}
  {@const n = p.weight == null ? null : Math.round(p.weight)}
  <span
    class="chip"
    class:faint={n != null && n <= 1}
    style:color={facetColour(p.facet)}
    style:border-color={facetColour(p.facet)}
    title={[p.gloss, showModel && n != null ? `suggested by ${n} source${n === 1 ? '' : 's'}` : null]
      .filter(Boolean)
      .join(' - ') || undefined}
    data-weight={n}
  >
    <span class="chiplabel">{termLabel(p)}</span>
    {#if showModel && n != null}
      <span class="n" aria-label={`${n} source${n === 1 ? '' : 's'}`}>{n}</span>
    {/if}
    {#if showModel}<span class="rawid">{p.term}</span>{/if}
  </span>
{/snippet}

<aside class="panel" aria-label="Title detail" use:dismiss={onClose}>
  <button class="close" onclick={onClose} aria-label="Close">✕</button>

  {#if error}
    <p class="err">{error}</p>
  {:else if !data}
    <p class="data">loading…</p>
  {:else}
    {@const t = data.title}
    <!-- Beside the name, not above it, so a phone keeps the actions above the fold. -->
    <div class="head">
      <div class="thumb"><RatePoster title={t} showName={false} /></div>
      <div class="head-text">
        <h2>{names.primary}</h2>
        {#if names.secondary}
          <div class="alt" data-testid="title-alt-name">{names.secondary}</div>
        {/if}
        <div class="data sub">{subline}</div>
      </div>
    </div>

    {#if why}
      <p class="whyline" data-testid="title-why">{why}</p>
    {/if}

    <div class="answers" role="group" aria-label="Your rating" data-testid="title-rate">
      {#each ANSWERS as a (a.answer)}
        <button
          class="pill answer"
          aria-pressed={a.answer !== 'not_seen' &&
            t.seen_state === 'seen' &&
            data.my_verdict?.label === a.answer}
          disabled={answering}
          data-answer={a.answer}
          onclick={() => answer(a.answer)}
        >
          {a.label}
        </button>
      {/each}
    </div>
    {#if answerNote}
      <p class="why note" role="status" data-testid="title-rate-note">{answerNote}</p>
    {/if}

    <div class="actions">
      <!-- Two states only (§4.2); this explicit action outranks what Jellyfin inferred (§7.3). -->
      <button
        class="btn-ghost seen"
        aria-pressed={t.seen_state === 'seen'}
        onclick={toggleSeen}
        disabled={saving}
        data-seen={t.seen_state ?? 'unseen'}
      >
        {t.seen_state === 'seen' ? 'Seen' : 'Mark seen'}
      </button>
      {#if data.actions.play_on_jellyfin}
        <a class="btn-primary" href={data.actions.play_on_jellyfin} target="_blank" rel="noreferrer">
          Play on Jellyfin
        </a>
      {:else}
        <button class="btn-primary" disabled>Play on Jellyfin</button>
      {/if}
      {#if data.actions.show_on_map}
        <!-- The server sends the target only once the Map ships. -->
        <a class="btn-ghost" href="/map?title={t.id}">Show on map</a>
      {/if}
    </div>
    {#if !data.actions.play_on_jellyfin}
      <!-- A visible line, not a title= tooltip: touch has no hover. -->
      <p class="why actionwhy" data-testid="title-jellyfin-why">
        {playWhy(data.actions.play_reason ?? 'no_server')}
      </p>
    {/if}
    {#if syncNote}
      <p class="why syncnote" role="status">{syncNote}</p>
    {/if}
    {#if t.kind === 'series' && t.seen_state === 'seen'}
      <!-- Un-marking a series would need a recursive DELETE over every episode, so it stays app-only. -->
      <div class="data seriesnote" data-testid="title-series-unseen-note">
        Marking a series not seen is kept in Spielplan only — Jellyfin is never told to un-play its
        episodes.
      </div>
    {/if}

    {#if t.overview}<p class="overview">{t.overview}</p>{/if}

    {#if t.trailer_key}
      <a
        class="trailer"
        href={`https://www.youtube.com/watch?v=${t.trailer_key}`}
        target="_blank"
        rel="noreferrer"
        aria-label="Watch the trailer on YouTube"
      >
        Watch the trailer
      </a>
    {/if}

    {#if data.model_line}
      <!-- The server's own `text`, so the card and the rail print the same number. -->
      <div class="modelline" data-testid="title-model-line">
        {#if data.model_line.available}
          <span class="data-lg">{data.model_line.text}</span>
          {#if data.model_line.second_line}
            <span class="data support">{data.model_line.second_line}</span>
          {/if}
          <span class="data source">{data.model_line.e_source} · bundle {data.model_line.bundle}</span>
        {:else}
          <span class="data-lg">model line unavailable — {data.model_line.reason}</span>
        {/if}
      </div>
    {/if}

    {#if topCredits.length}
      <section>
        <div class="data heading">CAST &amp; CREW</div>
        <div class="people">
          {#each topCredits as c (creditKey(c))}{@render creditRow(c)}{/each}
        </div>
      </section>
    {/if}

    <!-- A native <details>: the content stays in the document, and the browser owns the state. -->
    <details class="more" data-testid="title-more">
      <summary data-testid="title-more-toggle">More about this {kindNoun}</summary>

      {#if moreCredits.length}
        <section>
          <div class="data heading">
            MORE CAST &amp; CREW
            <span class="count" data-testid="credit-count"
              >{shownCredits.length.toLocaleString()} of {data.credits.length.toLocaleString()}</span
            >
          </div>
          <div class="people">
            {#each moreCredits as c (creditKey(c))}{@render creditRow(c)}{/each}
          </div>
          {#if data.credits.length > CREDIT_FOLD}
            <!-- Both labels use the constant, so raising CREDIT_FOLD cannot leave a stale word. -->
            <button
              class="btn-ghost disclose"
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

      {#if data.platform_ratings.items.length}
        <section>
          <div class="data heading">PLATFORM SCORES</div>
          <div class="scores">
            <!-- Keyed by platform and metric: one platform ships two scores on different scales. -->
            {#each data.platform_ratings.items as p (p.platform + ':' + p.metric)}
              <div class="score">
                <!-- The scale travels with the number: this block mixes 10- and 100-point scales. -->
                <span class="value">{round1(p.score)}<span class="of">/{round1(p.scale)}</span></span>
                <span class="data">{p.platform} · {metricLabel(p.metric)}</span>
              </div>
            {/each}
          </div>
          <p class="why">{data.platform_ratings.note}</p>
        </section>
      {/if}

      <!-- Two tiers, visibly distinct and never interleaved (§4.1 rule 1). -->
      <section>
        <div class="data heading">WHAT IT'S LIKE <span class="qv">each one quoted</span></div>
        {#if quoted.length}
          <!-- One block per term across providers; the id shows only with Show the model. -->
          {#each quoted as tag (tag.key)}
            <div class="tag" style:border-left-color={facetColour(tag.facet)}>
              <div class="tagline">
                <span class="term" style:color={facetColour(tag.facet)} title={tag.gloss ?? undefined}
                  >{termLabel(tag)}</span
                >
                {#if showModel}
                  <span class="data">{[
                    tag.term,
                    ...tag.rows.map((r) => (r.salience != null ? `sal ${r.salience}` : null))
                  ]
                    .filter(Boolean)
                    .join(' · ')}</span>
                {/if}
              </div>
              {#each tag.evidence as e}
                <div class="quote">“{quoteText(e.quote)}”</div>
                <div class="data src">{showModel ? e.source : sourceLabel(e.source)}</div>
              {/each}
            </div>
          {/each}
        {:else}
          <p class="why">No quoted tags for this title yet.</p>
        {/if}
      </section>

      <section>
        <div class="data heading">PROBABLY ALSO <span class="inferred">inferred, not quoted</span></div>
        {#if inferred.strong.length}
          <div class="chips">
            {#each inferred.strong as p (p.facet + ':' + p.term)}{@render chip(p)}{/each}
          </div>
        {/if}
        {#if inferred.weak.length}
          <!-- Folded, not dropped: a weight is never a filter (§4.1 rule 2). -->
          <details class="weak" data-testid="title-weak-chips">
            <summary>{inferred.weak.length} less certain</summary>
            <div class="chips">
              {#each inferred.weak as p (p.facet + ':' + p.term)}{@render chip(p)}{/each}
            </div>
          </details>
        {/if}
        {#if !data.dna.projected.length}
          <p class="why">Nothing inferred for this title yet.</p>
        {:else if !inferred.strong.length && !inferred.weak.length}
          <p class="why">Everything inferred is already quoted above.</p>
        {/if}
      </section>
    </details>
  {/if}
</aside>

<style>
  /* Below the header, which includes the installed app's status-bar inset. */
  .panel {
    position: fixed;
    top: calc(54px + env(safe-area-inset-top));
    right: 0;
    bottom: 0;
    width: min(420px, 100%);
    overflow: auto;
    background: var(--ground-raised);
    border-left: 1px solid var(--line);
    padding: 20px 20px 40px;
    z-index: 50;
    animation: fadeIn 0.14s ease;
  }
  /* design.css's coarse floor raises height only; add the width for fingers, not for a mouse. */
  .close {
    position: absolute;
    top: 14px;
    right: 16px;
    background: none;
    border: none;
    color: var(--ink-4);
    cursor: pointer;
    font-size: 14px;
    width: 32px;
    height: 32px;
  }
  @media (pointer: coarse) {
    .close {
      min-width: var(--touch);
    }
    /* A bare <a> gets no coarse floor from design.css; the base `.trailer` rule centres the label. */
    .trailer {
      min-height: var(--touch);
    }
  }
  /* The right margin clears the widest close control, --touch on a coarse pointer. */
  h2 {
    margin: 0 var(--touch) 4px 0;
    font-size: 19px;
    font-weight: 600;
  }
  .sub {
    margin-bottom: 10px;
  }
  .head {
    display: flex;
    gap: 14px;
    align-items: flex-start;
  }
  .thumb {
    width: 88px;
    flex: none;
  }
  .head-text {
    flex: 1;
    min-width: 0;
  }
  .alt {
    font-size: 13px;
    color: var(--ink-3);
    margin: 0 var(--touch) 4px 0;
  }
  .whyline {
    margin: 12px 0 10px;
    font-size: 14px;
    line-height: 1.45;
    color: var(--ink-2);
  }
  .more {
    border-top: 1px solid var(--line);
    margin-top: 6px;
  }
  .more > summary {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 2px;
    font-size: 13.5px;
    color: var(--ink-2);
    cursor: pointer;
    list-style: none;
  }
  .more > summary::-webkit-details-marker,
  .weak > summary::-webkit-details-marker {
    display: none;
  }
  .more > summary::after {
    content: '▾';
    color: var(--ink-4);
  }
  .more[open] > summary::after {
    content: '▴';
  }
  .more[open] > summary {
    margin-bottom: 8px;
  }
  .weak {
    margin-top: 8px;
  }
  .weak > summary {
    display: inline-flex;
    align-items: center;
    font-family: var(--mono);
    font-size: 11px;
    color: var(--ink-3);
    cursor: pointer;
    list-style: none;
    padding: 4px 0;
  }
  .weak[open] > summary {
    margin-bottom: 6px;
  }
  .weak > summary::after {
    content: '▾';
    margin-left: 6px;
  }
  .weak[open] > summary::after {
    content: '▴';
  }
  /* A summary is in none of design.css's coarse selectors, so the floor is set here. */
  @media (pointer: coarse) {
    .more > summary,
    .weak > summary {
      min-height: var(--touch);
    }
  }
  .answers {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin: 4px 0 8px;
  }
  .note {
    margin: 0 0 12px;
  }
  .overview {
    font-size: 13px;
    line-height: 1.55;
    color: var(--ink-2);
  }
  .trailer {
    display: inline-flex;
    gap: 8px;
    align-items: center;
    font-size: 12px;
    padding: 6px 10px;
    border: 1px solid var(--line-2);
    border-radius: var(--r-sm);
    margin-bottom: 18px;
  }
  .modelline {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    align-items: flex-start;
    padding: 9px 11px;
    border: 1px solid var(--line);
    border-radius: var(--r-sm);
    background: var(--card);
    margin: 12px 0;
  }
  .seriesnote {
    margin-top: -4px;
    color: var(--ink-4);
  }
  /* Pulled up under the actions, so the note belongs to their row. */
  .syncnote {
    margin: -10px 0 18px;
  }
  .actions {
    display: flex;
    gap: 8px;
    margin-bottom: 18px;
  }
  .actions a,
  .actions button {
    text-decoration: none;
    display: inline-flex;
    align-items: center;
  }
  /* Pulled up under the row it explains; `.actions` already carries the section gap. */
  .actionwhy {
    margin: -14px 0 18px;
  }
  section {
    margin-bottom: 20px;
  }
  .heading {
    letter-spacing: 0.12em;
    margin-bottom: 8px;
    display: flex;
    gap: 8px;
    align-items: baseline;
  }
  .qv {
    color: #5fae7a;
  }
  .inferred {
    color: var(--ink-4);
  }
  .count {
    margin-left: auto;
    color: var(--ink-3);
    letter-spacing: normal;
  }
  .disclose {
    margin-top: 8px;
    font-size: 12px;
  }
  .people {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .person {
    display: flex;
    align-items: center;
    gap: 9px;
    padding: 7px 8px;
    border: none;
    background: none;
    border-radius: var(--r-sm);
    cursor: pointer;
    color: inherit;
    text-align: left;
  }
  .person:hover {
    background: rgba(255, 255, 255, 0.05);
  }
  .dot {
    width: 26px;
    height: 26px;
    border-radius: 50%;
    display: grid;
    place-items: center;
    background: var(--card-raised);
    font-size: 11px;
    flex: none;
  }
  .pname {
    font-size: 12.5px;
    flex: 1;
  }
  .scores {
    display: flex;
    flex-wrap: wrap;
    gap: 10px 18px;
  }
  .score {
    display: flex;
    flex-direction: column;
  }
  .value {
    font-family: var(--mono);
    font-size: 17px;
  }
  .of {
    font-size: 12px;
    color: var(--ink-4);
  }
  .tag {
    border-left: 2px solid var(--ink-4);
    padding: 7px 0 7px 10px;
    margin-bottom: 8px;
    background: var(--card);
    border-radius: 0 var(--r-sm) var(--r-sm) 0;
  }
  .tagline {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 8px;
  }
  .term {
    font-family: var(--mono);
    font-size: 11px;
  }
  .quote {
    font-size: 12.5px;
    color: var(--ink-2);
    font-style: italic;
    margin: 4px 8px 2px 0;
    line-height: 1.45;
  }
  .src {
    color: var(--ink-5);
  }
  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .chip {
    display: inline-flex;
    gap: 5px;
    align-items: baseline;
    font-family: var(--mono);
    font-size: 10px;
    padding: 4px 9px;
    border: 1px solid;
    border-radius: var(--r-pill);
    opacity: 0.85;
  }
  /* One source behind it: fainter, never absent (§4.1 rule 2). */
  .chip.faint {
    border-style: dashed;
    opacity: 0.55;
  }
  .n,
  .rawid {
    color: var(--ink-4);
  }
  .err {
    color: var(--ember-lift);
  }

  @media (max-width: 720px) {
    .panel {
      /* Restate the inset: this override wins on phones, the device it is for. */
      top: calc(54px + env(safe-area-inset-top));
      width: 100%;
      border-left: none;
    }
  }
</style>
