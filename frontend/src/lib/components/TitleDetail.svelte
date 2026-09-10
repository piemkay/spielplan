<script>
  /**
   * The title detail card. Spec v2.1 §6.0:
   *   "metadata; credits, each person tappable → filters the library to their filmography;
   *    trailer key; platform scores (display-only schema, labelled as such); the DNA card —
   *    tags with evidence quotes, extracted/projected tier visibly distinct (§4.1 rule 1);
   *    the model line in the data voice (`b(t) 0.52 · β 0.8 · gate 0.93`); and two actions —
   *    Play on Jellyfin (§7.1) and Show on map (§6.4)."
   *
   * The prototype's version of this card omitted the model line and Play on Jellyfin; both are
   * here, and each degrades to an honest disabled state rather than disappearing when the
   * thing behind it (a bundle, a Jellyfin link) does not exist yet.
   */
  import { get, post } from '$lib/api.js';
  // The palette and the runtime label are shared, not copied. This file held a second FACETS set
  // and a second `facetColour`, and it was the copy that rotted: two spellings of one palette is
  // how one of them stops matching the data. Same argument for `runtimeLabel`, which existed
  // here without the kind branch the other two copies had, so a series read `0h 24m` two taps
  // after a poster that said `24m/ep`. [M4.9 findings 3, 4, 37]
  import { facetColour } from '$lib/home.svelte.js';
  import { runtimeLabel } from '$lib/rate.svelte.js';

  let { titleId, onClose, onPerson, onStateChange } = $props();

  let data = $state(null);
  // Two separate channels on purpose. `error` is the *load* failing, and the template replaces
  // the whole card with it; an action failing must not take the title, the credits and both DNA
  // tiers off the screen with it.
  let error = $state('');
  let syncNote = $state('');
  let saving = $state(false);
  // The collapsed default, and the twelve that fit under it. Twelve is what shipped; what was
  // missing is that the card never said it was twelve of anything. [M4.9 finding 7]
  const CREDIT_FOLD = 12;
  let showAllCredits = $state(false);

  // The corpus stores a platform score at full float precision — trakt's is 9.167481422424316 —
  // and a card that prints sixteen digits is claiming a precision nobody has. One decimal,
  // trailing zero dropped, so a 100-point score reads `89` and a 10-point one `9.2`.
  const round1 = (n) => (n === null || n === undefined ? '' : Number(Number(n).toFixed(1)));
  // `user_score` / `critic_score` / `audience_rating_count` are the corpus's own metric names.
  const metricLabel = (m) => String(m ?? '').replace(/_/g, ' ');

  // Re-fetch whenever the panel is pointed at a different title. On mount alone, tapping a
  // second poster while the panel is open left the first title's card on screen.
  $effect(() => {
    const id = titleId;
    let cancelled = false;
    data = null;
    error = '';
    syncNote = '';
    // Reset with the rest: an expanded list carried into the next title would show the previous
    // film's credit count against this film's people for as long as the fetch takes.
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

  /**
   * §7.3: "App is authoritative for explicit user actions" — this is that action. The app-side
   * write never depends on Jellyfin, so the response also carries whether the media server was
   * told, and `syncNote` says so plainly rather than pretending it succeeded.
   */
  async function toggleSeen() {
    if (!data || saving) return;
    const next = data.title.seen_state === 'seen' ? 'unseen' : 'seen';
    saving = true;
    syncNote = '';
    try {
      const res = await post(`/titles/${data.title.id}/state`, { state: next });
      data = { ...data, title: { ...data.title, seen_state: next } };
      syncNote = res.synced ? 'synced to Jellyfin' : res.reason || '';
      onStateChange?.(data.title.id, next);
    } catch (err) {
      syncNote = `could not save that — ${err.message}`;
    } finally {
      saving = false;
    }
  }

  // §6.0's metadata line, through the one label. This copy had no kind branch at all, so the
  // subline said `2017 · 0h 24m · series` about the same episode the card behind it called
  // `24m/ep`. [M4.9 finding 37]
  const runtime = $derived(runtimeLabel(data?.title));
  // The whole list is already on the client — `credits_for` returns every row and the card kept
  // twelve. The disclosure spends what the payload holds; it does not fetch, and no query grew
  // a LIMIT to make it possible.
  const shownCredits = $derived(
    showAllCredits ? (data?.credits ?? []) : (data?.credits ?? []).slice(0, CREDIT_FOLD)
  );
  // Joined in JS — Svelte collapses whitespace around {#if} blocks in markup.
  const subline = $derived(
    data
      ? [data.title.year ?? '—', runtime, data.title.kind,
         data.title.seen_state === 'seen' ? 'seen' : null].filter(Boolean).join(' · ')
      : ''
  );
</script>

<aside class="panel" aria-label="Title detail">
  <button class="close" onclick={onClose} aria-label="Close">✕</button>

  {#if error}
    <p class="err">{error}</p>
  {:else if !data}
    <p class="data">loading…</p>
  {:else}
    {@const t = data.title}
    <h2>{t.name}</h2>
    <div class="data sub">{subline}</div>
    {#if t.original_name && t.original_name !== t.name}
      <div class="data">{t.original_name}</div>
    {/if}

    {#if t.overview}<p class="overview">{t.overview}</p>{/if}

    {#if t.trailer_key}
      <!-- §6.0 lists the trailer key as M0 content on the card. -->
      <a
        class="trailer"
        href={`https://www.youtube.com/watch?v=${t.trailer_key}`}
        target="_blank"
        rel="noreferrer"
      >
        <span class="data">TRAILER</span>
        <span>{t.trailer_key}</span>
      </a>
    {/if}

    <!-- §6.0: the model line, in the data voice, never bare: `b(t) 0.52 · β 0.8 · gate 0.93`.
         Rendered from the server's own `text`, not recomposed here, so the card and §6.7's rail
         print the same number to the same precision.

         NOT gated by decision 117's "show the model" toggle, deliberately. §6.0 lists this line
         unconditionally as the M0 transparency promise, and proposal 19 says so again: it is the
         one place a model number is part of the product rather than part of the debugging. -->
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

    <div class="actions">
      <!-- §4.2: two states and only two — there is no 'forgotten' (owner decision
           2026-08-29). §7.3: this explicit action outranks whatever Jellyfin inferred. -->
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
        <button class="btn-primary" disabled title="link a Jellyfin server in Admin (M1)">
          Play on Jellyfin
        </button>
      {/if}
      <a class="btn-ghost" href="/map?title={t.id}">Show on map</a>
    </div>
    {#if syncNote}
      <div class="data syncnote" role="status">{syncNote}</div>
    {/if}

    {#if data.credits.length}
      <section>
        <!-- §6.0 applies a count-line discipline to the kind toggle — "with one active the count
             line says how many the other holds" — and this surface ignored it: twelve of a
             median twenty-four credits rendered with nothing saying so, and 89.7% of corpus
             titles carry more than twelve, so a writer, composer or cinematographer was simply
             absent. The line is the data voice, the collapsed twelve stay the default, and the
             disclosure reveals the rest of a list the client already holds — no route change and
             no LIMIT in `credits_for`, because the payload was never the problem.

             Both counts carry their separators, as `countLabel`'s do: the corpus runs to 1,535
             credits on one title against a median of 24, and `1535` in a data-voice line is the
             same number the catalogue two screens away writes `1,535`.
             [M4.9 finding 7 / cs-23; review cycle 1] -->
        <div class="data heading">
          CAST &amp; CREW
          <span class="count" data-testid="credit-count"
            >{shownCredits.length.toLocaleString()} of {data.credits.length.toLocaleString()}</span
          >
        </div>
        <div class="people">
          <!-- Keyed by person AND job, delimited: `credits_for` collapses to one row per
               (person, job), and the delimiter is what stops person 700 + job `1Actor` colliding
               with person 7001 + job `Actor`. The undelimited key threw on 1,216 real titles
               where one person held one job under two department spellings, and with no
               +error.svelte the whole card died mid-render. Keyed, not unkeyed: the key is what
               keeps `onPerson` attached to the right person. -->
          {#each shownCredits as c (c.person_id + ':' + c.job)}
            <button class="person" onclick={() => onPerson(c)}>
              <span class="dot">{c.name.charAt(0)}</span>
              <span class="pname">{c.name}</span>
              <span class="data"
                >{[c.job, c.sources?.length > 1 ? `${c.sources.length} sources` : null]
                  .filter(Boolean)
                  .join(' · ')}</span
              >
            </button>
          {/each}
        </div>
        {#if data.credits.length > CREDIT_FOLD}
          <!-- `btn-ghost` rather than a local size: design.css grows every interactive primitive
               to var(--touch) = 48px under `pointer: coarse`, which is §6's phone-first rule
               stated once instead of re-picked here.

               Both labels are the constant rather than a word for it. `Show twelve` was a second
               spelling of `CREDIT_FOLD` in English, which is the one spelling an edit to the
               constant cannot reach: raise the fold and the button keeps saying twelve while the
               count line above it says otherwise. [M4.9 review cycle 1] -->
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
          <!-- Keyed by platform AND metric: since 0015 the row is per (platform, metric), and
               metacritic ships a critic score and a user score on different scales — one key
               per platform silently dropped the second and made Svelte's keyed each throw. -->
          {#each data.platform_ratings.items as p (p.platform + ':' + p.metric)}
            <div class="score">
              <!-- §6.0: the caption travels with the number. 89 is not a score until the line
                   also says out of 100, and this block mixes 10-point and 100-point scales. -->
              <span class="value">{round1(p.score)}<span class="of">/{round1(p.scale)}</span></span>
              <span class="data">{p.platform} · {metricLabel(p.metric)}</span>
            </div>
          {/each}
        </div>
        <!-- §4.1 rule 3, printed where it is relevant rather than buried in a doc. -->
        <p class="why">{data.platform_ratings.note}</p>
      </section>
    {/if}

    <!-- §4.1 rule 1: the two tiers are visibly distinct, and never interleaved. -->
    <section>
      <div class="data heading">DNA — EXTRACTED <span class="qv">quote-verified</span></div>
      {#if data.dna.extracted.length}
        <!-- Keyed on facet, term AND PROVIDER, delimited. The crash is the platform-scores
             block's above: `dna_tag` is unique on (title_id, version, term, provider), so §6.6's
             parallel extraction mode writes one term twice and Svelte's keyed each raises
             `each_key_duplicate` in the production build too. The provider is the component that
             does that work — since 0018 section 1 the facet IS `split_part(term, '.', 1)`, so
             facet and term together separate exactly what the term separated alone, which is
             nothing at all for the one pair of rows this key exists to keep apart. NOT
             de-duplicated here — §4.1 rule 1 and §6.6 both want both rows visible; the key is
             what makes two rows two rows. [M4.9 finding 8; review cycle 1]

             `{tag.term}` alone: §4.3's vocabulary id IS `facet.term`, so the shipped term
             already carries its prefix and printing the facet again read
             `narrative_themes.themes.love_romance`. The facet is spent on the colour, which is
             the identity §6.8 asks for. [M4.9 finding 3] -->
        {#each data.dna.extracted as tag (tag.facet + ':' + tag.term + ':' + tag.provider)}
          <div class="tag" style:border-left-color={facetColour(tag.facet)}>
            <div class="tagline">
              <span class="term" style:color={facetColour(tag.facet)}>{tag.term}</span>
              <span class="data">sal {tag.salience}</span>
            </div>
            {#each tag.evidence as e}
              <div class="quote">“{e.quote}”</div>
              <div class="data src">{e.source}</div>
            {/each}
          </div>
        {/each}
      {:else}
        <p class="why">No extracted tags yet — this title has not been through DNA extraction.</p>
      {/if}
    </section>

    <section>
      <div class="data heading">DNA — PROJECTED (INFERRED)</div>
      {#if data.dna.projected.length}
        <div class="chips">
          <!-- Same label rule as the extracted tier above, and one key component fewer:
               `dna_projected` is UNIQUE (title_id, version, term), so no second provider can put
               one term on this list twice and the term is a key here on its own merits.
               [M4.9 review cycle 1] -->
          {#each data.dna.projected as p (p.facet + ':' + p.term)}
            <span class="chip" style:color={facetColour(p.facet)} style:border-color={facetColour(p.facet)}>
              {p.term}
            </span>
          {/each}
        </div>
      {:else}
        <p class="why">No projected tags.</p>
      {/if}
    </section>
  {/if}
</aside>

<style>
  .panel {
    position: fixed;
    top: 54px;
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
  h2 {
    margin: 0 32px 4px 0;
    font-size: 19px;
    font-weight: 600;
  }
  .sub {
    margin-bottom: 10px;
  }
  .overview {
    font-size: 13px;
    line-height: 1.55;
    color: var(--ink-2);
  }
  .trailer {
    display: inline-flex;
    gap: 8px;
    align-items: baseline;
    font-family: var(--mono);
    font-size: 11px;
    padding: 6px 10px;
    border: 1px solid var(--line-2);
    border-radius: var(--r-sm);
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
  .syncnote {
    margin-top: -4px;
    color: var(--ink-4);
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
  /* The count rides in the heading, at the heading's own weight: it is a fact about the list,
     not a control. §6.8's data voice is already on `.heading`. */
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
  /* A title with every source resolved carries ten scored rows, not the two the single-metric
     key used to produce, so the row wraps rather than overflowing the 420px panel. */
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
  /* The scale is part of the number, not a second fact: same line, quieter. */
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
    font-family: var(--mono);
    font-size: 10px;
    padding: 4px 9px;
    border: 1px solid;
    border-radius: var(--r-pill);
    opacity: 0.85;
  }
  .err {
    color: var(--ember-lift);
  }

  @media (max-width: 720px) {
    .panel {
      top: 54px;
      width: 100%;
      border-left: none;
    }
  }
</style>
