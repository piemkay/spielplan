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
  import { get } from '$lib/api.js';

  let { titleId, onClose, onPerson } = $props();

  let data = $state(null);
  let error = $state('');

  // Vocabulary v1's eleven facets (§6.8: "a fixed colour per vocabulary facet (11)").
  const FACETS = new Set([
    'mood', 'themes', 'pacing', 'structure', 'visual', 'sound',
    'character', 'place', 'era', 'sensibility', 'register'
  ]);
  // An unknown facet gets a neutral, never the ember: §6.8 spends the accent on selection and
  // primary actions only, so a stray tag must not borrow it.
  const facetColour = (f) => (FACETS.has(f) ? `var(--facet-${f})` : 'var(--ink-4)');

  // Re-fetch whenever the panel is pointed at a different title. On mount alone, tapping a
  // second poster while the panel is open left the first title's card on screen.
  $effect(() => {
    const id = titleId;
    let cancelled = false;
    data = null;
    error = '';
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

  const runtime = $derived(
    data?.title?.runtime_min
      ? `${Math.floor(data.title.runtime_min / 60)}h ${data.title.runtime_min % 60}m`
      : null
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

    <!-- §6.0: the model line, in the data voice, never bare. -->
    <div class="modelline">
      {#if data.model_line.available}
        <span class="data-lg">bundle {data.model_line.bundle}</span>
      {:else}
        <span class="data-lg">model line unavailable — {data.model_line.reason}</span>
      {/if}
    </div>

    <div class="actions">
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

    {#if data.credits.length}
      <section>
        <div class="data heading">CAST &amp; CREW</div>
        <div class="people">
          {#each data.credits.slice(0, 12) as c (c.person_id + c.job)}
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
      </section>
    {/if}

    {#if data.platform_ratings.items.length}
      <section>
        <div class="data heading">PLATFORM SCORES</div>
        <div class="scores">
          {#each data.platform_ratings.items as p (p.platform)}
            <div class="score">
              <span class="value">{p.score}</span>
              <span class="data">{p.platform}</span>
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
        {#each data.dna.extracted as tag (tag.term)}
          <div class="tag" style:border-left-color={facetColour(tag.facet)}>
            <div class="tagline">
              <span class="term" style:color={facetColour(tag.facet)}>{tag.facet}.{tag.term}</span>
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
          {#each data.dna.projected as p (p.term)}
            <span class="chip" style:color={facetColour(p.facet)} style:border-color={facetColour(p.facet)}>
              {p.facet}.{p.term}
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
    padding: 9px 11px;
    border: 1px solid var(--line);
    border-radius: var(--r-sm);
    background: var(--card);
    margin: 12px 0;
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
    gap: 18px;
  }
  .score {
    display: flex;
    flex-direction: column;
  }
  .value {
    font-family: var(--mono);
    font-size: 17px;
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
