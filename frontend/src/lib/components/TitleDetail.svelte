<script>
  // A sheet (decision 527) that leads with what a member opens it for; the rest of §6.0's card
  // (credits, scores, both DNA tiers) sits behind one disclosure. Model numbers arrive only with
  // Show the model on.
  import { get, post } from '$lib/api.js';
  import { facetColour, modelGate } from '$lib/home.svelte.js';
  import { seed as seedPlace } from '$lib/place.svelte.js';
  import { cardMove } from '$lib/rank.svelte.js';
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
  import ActionSheet from '$lib/components/ActionSheet.svelte';
  import AnswerTiles from '$lib/components/AnswerTiles.svelte';
  import Headshot from '$lib/components/Headshot.svelte';
  import RatePoster from '$lib/components/RatePoster.svelte';
  import Sheet from '$lib/components/Sheet.svelte';

  // `seed`: the title as the tapped poster had it, so the card opens on its poster and name before
  // the read lands. `onMove(entry, tier)`: Rank's own move, which also replaces its board; anywhere
  // else the card drops the title itself (decision 531).
  let { titleId, seed = undefined, onClose, onPerson, onStateChange, onMove = undefined } = $props();

  let open = $state(true);
  let data = $state(null);
  // `error` is the load failing and replaces the card; an action failing must not.
  let error = $state('');
  let syncNote = $state('');
  let saving = $state(false);
  // Watched just now, so its check draws; one already watched shows it drawn.
  let justWatched = $state(false);
  let answerNote = $state(null);
  let answering = $state(null);
  let refused = $state(null);
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
    answerNote = null;
    justWatched = false;
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
      justWatched = next === 'seen';
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
    answering = choice;
    answerNote = null;
    refused = null;
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
      answerNote = { saved: answeredLine(choice), reveal: revealLine(res?.reveal) };
      onStateChange?.(data.title.id, next);
    } catch (err) {
      answerNote = { saved: `Could not save that — ${err.message}`, reveal: '' };
      refused = choice;
    } finally {
      answering = null;
    }
  }

  // §6.3's two rows; `tier` is null until the title is on the person's own board.
  let choosing = $state(false);
  const ranking = $derived(data?.ranking ?? null);
  const placedTier = $derived(ranking?.tiers.find((tier) => tier.index === ranking.tier) ?? null);
  // About log2(n) either-or questions place a title inside its tier.
  const questions = $derived(placedTier ? Math.ceil(Math.log2(placedTier.count)) : 0);
  const moveOptions = $derived(
    (ranking?.tiers ?? []).map((tier) => ({
      label: tier.label,
      detail: tier.verdict || undefined,
      checked: tier.index === ranking.tier,
      onSelect: () => chooseTier(tier)
    }))
  );

  // A tier implies seen, and on an unrated title answers the verdict it stands for (decision 531).
  async function chooseTier(tier) {
    const t = data.title;
    const entry = { title_id: t.id, name: t.name, kind: t.kind, tier: data.ranking.tier };
    if (!(await (onMove ?? cardMove)(entry, tier))) return;
    const added = !data.my_verdict || t.seen_state !== 'seen';
    const verdict = tier.verdict.toLowerCase();
    data = {
      ...data,
      title: { ...data.title, seen_state: 'seen' },
      my_verdict: data.my_verdict ?? { value: ['disliked', 'fine', 'liked'].indexOf(verdict), label: verdict },
      // The tension line described the old placement.
      ranking: { ...data.ranking, tier: tier.index, tension: null },
      why: null
    };
    if (added) onStateChange?.(t.id, 'seen');
  }

  const lead = $derived(data?.title ?? seed ?? null);
  const runtime = $derived(runtimeLabel(lead));
  // `credits_for` returns every row; the disclosure spends what the payload holds.
  const shownCredits = $derived(
    showAllCredits ? (data?.credits ?? []) : (data?.credits ?? []).slice(0, CREDIT_FOLD)
  );
  // One list read in two places, so the count line counts every row shown.
  const topCredits = $derived(shownCredits.slice(0, CREDIT_TOP));
  const moreCredits = $derived(shownCredits.slice(CREDIT_TOP));
  const names = $derived(displayNames(lead));
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
  const subline = $derived(lead ? [lead.year ?? '—', runtime].filter(Boolean).join(' · ') : '');
  const pressed = (a) =>
    a.answer !== 'not_seen' &&
    data?.title.seen_state === 'seen' &&
    data?.my_verdict?.label === a.answer;
</script>

{#snippet icon(name, size = 20)}
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width={name === 'close' ? 2.25 : 1.75} stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    {#if name === 'play'}
      <path d="M8 5.5v13l10.5-6.5z" fill="currentColor" stroke="none" />
    {:else if name === 'trailer'}
      <circle cx="12" cy="12" r="8.5" /><path d="M10 8.8v6.4l5-3.2z" fill="currentColor" stroke="none" />
    {:else if name === 'check'}
      <path d="m5 12.5 4.5 4.5L19 7.5" pathLength="1" />
    {:else if name === 'close'}
      <path d="M6 6l12 12M18 6 6 18" />
    {:else if name === 'chevron'}
      <path d="m9.5 5.5 6.5 6.5-6.5 6.5" />
    {/if}
  </svg>
{/snippet}

<!-- Callers key rows by `creditKey` (person and role class), so `onPerson` stays attached. -->
{#snippet person(c, close, chevron = false)}
  <button
    class="person"
    onclick={() => {
      afterClose = () => onPerson(c);
      close();
    }}
  >
    <Headshot credit={c} />
    <span class="who">
      <span class="pname">{c.name}</span>
      <span class="job"
        >{[c.character || creditJobs(c), showModel && c.sources?.length > 1 ? `${c.sources.length} sources` : null]
          .filter(Boolean)
          .join(' · ')}</span
      >
    </span>
    {#if chevron}{@render icon('chevron', 16)}{/if}
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
    {:else if !lead}
      <p class="footnote loading">Loading…</p>
    {:else}
      {@const t = lead}
      <div class="detail">
        <div class="lead">
          <div class="art"><RatePoster title={t} showName={false} /></div>

          <div class="head">
            <h2 class="title-1">{names.primary}</h2>
            {#if names.secondary}
              <p class="alt" data-testid="title-alt-name">{names.secondary}</p>
            {/if}
            <p class="sub">{subline}</p>
            {#if genreLine(data?.genres)}
              <p class="alt" data-testid="title-genres">{genreLine(data?.genres)}</p>
            {/if}
            {#if directed}<p class="footnote" data-testid="title-directed">{directed}</p>{/if}
          </div>

          <div class="main" class:arrive={seed && data}>
            {#if !data}
              <div class="pending" aria-hidden="true">
                <span></span><span class="play"></span><span class="label"></span><span class="tiles"></span>
              </div>
            {:else}
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

              {#if ranking}
                <div class="list-group ranking">
                  <button
                    class="list-row"
                    aria-haspopup="dialog"
                    aria-label="In your ranking: {placedTier
                      ? `${placedTier.label}${placedTier.verdict ? `, ${placedTier.verdict}` : ''}`
                      : 'not placed yet'}"
                    onclick={() => (choosing = true)}
                    data-testid="rank-card-tier"
                  >
                    <span class="grow">In your ranking</span>
                    {#if placedTier}
                      <span class="letter">{placedTier.label}</span>
                      {#if placedTier.verdict}<span class="footnote">{placedTier.verdict}</span>{/if}
                    {:else}
                      <span class="footnote">Not placed yet</span>
                    {/if}
                    {@render icon('chevron', 16)}
                  </button>
                  {#if questions > 0}
                    <a
                      class="list-row"
                      href="/rank/place/{t.id}?kind={t.kind}"
                      onclick={() => seedPlace({ title_id: t.id, name: t.name })}
                      data-testid="rank-card-place"
                    >
                      <span class="grow">Place with questions</span>
                      <span class="footnote">{questions} quick {questions === 1 ? 'question' : 'questions'}</span>
                      {@render icon('chevron', 16)}
                    </a>
                  {/if}
                </div>
                {#if ranking.tension}
                  <p class="list-footer" data-testid="rank-card-tension">{ranking.tension}</p>
                {/if}
              {/if}

              <div class="answerblock">
                <h3 class="list-header">Your answer</h3>
                <AnswerTiles
                  answers={ANSWERS}
                  label="Your answer"
                  testid="title-rate"
                  compact
                  pending={answering}
                  {pressed}
                  failed={refused}
                  onAnswer={(a) => answer(a.answer)}
                />
                <!-- Always there, so a note arriving pushes nothing under it down. -->
                <p class="footnote status" role="status" data-testid="title-rate-note">
                  {#if answerNote}
                    <span class="beat">{answerNote.saved}</span>
                    <span class="beat reveal">{answerNote.reveal}</span>
                  {:else if !data.my_verdict}
                    You haven't rated this yet.
                  {/if}
                </p>
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
                  class:drawn={justWatched}
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
              {#if t.kind === 'series'}
                <!-- A Played write on a series would rewrite every episode, so it stays app-only both ways (decision 532). -->
                <p class="footnote" data-testid="title-series-unseen-note">
                  Watched or not, a series is kept in Spielplan only — Jellyfin keeps its own episode
                  history.
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
        <details class="more" data-testid="title-more" hidden={!data}>
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
                  {#each moreCredits as c (creditKey(c))}<li>{@render person(c, close, true)}</li>{/each}
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

<!-- After the card, so the tier sheet opens over it. -->
<ActionSheet
  open={choosing}
  title={data ? `${data.ranking?.tier == null ? 'Rank' : 'Move'} ${data.title.name}` : ''}
  options={moveOptions}
  onClose={() => (choosing = false)}
/>

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
  .pending {
    display: flex;
    flex-direction: column;
    gap: 16px;
    animation: enter var(--dur-quick) var(--ease) 150ms both;
  }
  .pending > span {
    display: block;
    width: 70%;
    height: 20px;
    border-radius: var(--r-sm);
    background: var(--surface-1);
  }
  .pending > .play,
  .pending > .tiles {
    width: auto;
    height: 50px;
    border-radius: var(--r-md);
  }
  .pending > .label {
    width: 30%;
    height: 18px;
  }
  .pending > .tiles {
    height: 60px;
  }
  .arrive > * {
    animation: fadeIn var(--dur-base) var(--ease) both;
  }
  .arrive > :nth-child(2) {
    animation-delay: 30ms;
  }
  .arrive > :nth-child(3) {
    animation-delay: 60ms;
  }
  .arrive > :nth-child(4) {
    animation-delay: 90ms;
  }
  .arrive > :nth-child(n + 5) {
    animation-delay: 120ms;
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
  .ranking .list-row {
    gap: 8px;
    padding-right: 12px;
    color: var(--text);
  }
  .ranking .grow {
    flex: 1;
    min-width: 0;
  }
  .letter {
    min-width: 28px;
    height: 28px;
    flex: none;
    padding: 0 3px;
    display: grid;
    place-items: center;
    border-radius: 8px;
    background: var(--surface-3);
    font-family: var(--serif);
    font-size: var(--fs-section);
    line-height: 22px;
    color: var(--text);
  }
  .ranking .footnote {
    flex: none;
  }
  .ranking svg {
    flex: none;
    color: var(--text-3);
  }
  .status {
    min-height: 18px;
  }
  .beat {
    animation: fadeIn var(--dur-quick) var(--ease) both;
  }
  .beat.reveal {
    animation-delay: 160ms;
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
  .seen[aria-busy='true'] {
    opacity: 0.6;
    transition-delay: 120ms;
  }
  .seen[aria-pressed='true'] > svg {
    color: var(--positive);
  }
  .seen.drawn path {
    stroke-dasharray: 1;
    animation: draw var(--dur-slow) var(--ease) both;
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
    --face: 72px;
    display: flex;
    gap: 12px;
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
    color: var(--text-3);
  }
  .strip .person {
    width: 88px;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 8px;
    text-align: center;
  }
  .strip .who {
    width: 100%;
  }
  .strip .pname {
    padding: 0 4px;
    font-weight: 600;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .strip .job,
  .list .who > span {
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .list {
    --face: 40px;
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
    min-height: 56px;
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 8px 12px 8px var(--gutter);
  }
  .list .who {
    flex: 1;
  }
  .list .person > :global(svg) {
    flex: none;
    color: var(--text-3);
  }
  .list .pname {
    font-size: var(--fs-body);
    line-height: 22px;
  }
  .list .job {
    font-size: var(--fs-footnote);
    line-height: 18px;
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
      --face: 80px;
      margin: 0;
      padding: 0;
      gap: 16px;
    }
    .strip .person {
      width: 96px;
    }
  }
</style>
