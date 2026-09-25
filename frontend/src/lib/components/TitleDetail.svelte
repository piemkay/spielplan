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
   *
   * Since the 2026-09-25 user test it speaks the member register (decision 486): the model line
   * and every weight arrive only while the viewer's Show the model is on, because the server
   * leaves them out otherwise; terms are named by their label; and every sentence under an action
   * is a plain one. It also answers the title itself - Liked / Fine / Disliked / Not seen, through
   * §6.1's own session (decision 487) - and Show on map waits, absent, for §6.4's Map
   * (decision 488).
   */
  import { get, post } from '$lib/api.js';
  // The palette and the runtime label are shared, not copied. This file held a second FACETS set
  // and a second `facetColour`, and it was the copy that rotted: two spellings of one palette is
  // how one of them stops matching the data. Same argument for `runtimeLabel`, which existed
  // here without the kind branch the other two copies had, so a series read `0h 24m` two taps
  // after a poster that said `24m/ep`. [M4.9 findings 3, 4, 37]
  import { facetColour, modelGate } from '$lib/home.svelte.js';
  import { KIND_LABELS, runtimeLabel } from '$lib/rate.svelte.js';
  import { session } from '$lib/session.svelte.js';
  import { termLabel } from '$lib/terms.js';
  import {
    ANSWERS,
    answeredLine,
    creditJobs,
    creditKey,
    playWhy,
    quoteText,
    revealLine,
    sourceLabel,
    syncNote as syncNoteFor
  } from '$lib/titleCard.js';
  import { dismiss } from '$lib/dismiss.js';
  import RatePoster from '$lib/components/RatePoster.svelte';

  let { titleId, onClose, onPerson, onStateChange } = $props();

  let data = $state(null);
  // Two separate channels on purpose. `error` is the *load* failing, and the template replaces
  // the whole card with it; an action failing must not take the title, the credits and both DNA
  // tiers off the screen with it.
  let error = $state('');
  let syncNote = $state('');
  let saving = $state(false);
  // Decision 487's answer row has its own line, for the same reason the seen toggle does.
  let answerNote = $state('');
  let answering = $state(false);
  // What the viewer asked to see. The server has already left the numbers out when this is off,
  // so this decides only the few labels that sit beside data the payload always carries (a
  // term's raw id, a credit's source count, an evidence key).
  const showModel = $derived(!!session.user?.show_model);
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
  //
  // And whenever Show the model settles, because the model line is absent from the payload rather
  // than hidden in it (decision 486): the only way to show it is to ask again. `modelGate.epoch`
  // and not the local flag, for the reason `routes/+page.svelte` gives - the epoch moves once the
  // server has the preference, and a refetch on the optimistic flip would race the write.
  $effect(() => {
    const id = titleId;
    void modelGate.epoch;
    let cancelled = false;
    data = null;
    error = '';
    syncNote = '';
    answerNote = '';
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
      // The reason wins when there is one, even on a success. Decision 210(a) produces exactly
      // that pair: marking a series not-seen is app-only — no recursive DELETE goes to the Series
      // folder — and `seen.set_state` reports it as `synced: true` with the reason
      // "series unseen is app-only", because the row is settled and nothing is owed. Reading only
      // `synced` printed "synced to Jellyfin" about a write that was deliberately never sent.
      // `syncNoteFor` keeps that order and says it in the member register (decision 486).
      syncNote = syncNoteFor(res);
      onStateChange?.(data.title.id, next);
    } catch (err) {
      syncNote = `Could not save that — ${err.message}`;
    } finally {
      saving = false;
    }
  }

  /**
   * Decision 487: the card's answer to the title, written as §6.1's sweep answer. The route puts
   * the title on the person's own Rate table and answers it there, so this tap has the journal
   * row Undo reverses on Rate, the block counter, the §7.3 push and the reveal - which arrives in
   * the response to the tap and in no earlier one, §6.1's anchoring rule.
   */
  async function answer(choice) {
    if (!data || answering) return;
    answering = true;
    answerNote = '';
    try {
      const res = await post(`/rate/title/${data.title.id}`, { answer: choice });
      const next = choice === 'not_seen' ? 'unseen' : 'seen';
      data = {
        ...data,
        title: { ...data.title, seen_state: next },
        // §4.2: Not seen writes a state and no observation, so the verdict it follows survives
        // the flip; a verdict supersedes the last one. The stored ordinal is the label's index.
        my_verdict:
          choice === 'not_seen'
            ? data.my_verdict
            : { value: ['disliked', 'fine', 'liked'].indexOf(choice), label: choice }
      };
      answerNote = [answeredLine(choice), revealLine(res?.reveal)].filter(Boolean).join(' ');
      onStateChange?.(data.title.id, next);
    } catch (err) {
      answerNote = `Could not save that — ${err.message}`;
    } finally {
      answering = false;
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
  // Joined in JS — Svelte collapses whitespace around {#if} blocks in markup. The kind by the
  // word the Rate card uses for it, not the enum (decision 486): `movie` is a column value.
  const subline = $derived(
    data
      ? [data.title.year ?? '—', runtime, KIND_LABELS[data.title.kind] ?? data.title.kind,
         data.title.seen_state === 'seen' ? 'seen' : null].filter(Boolean).join(' · ')
      : ''
  );
</script>

<!-- Proposal 131's outside tap and Escape, through the one action. On a phone this panel is the
     whole screen (`width: min(420px, 100%)` and full-bleed under 720 px), and until now the only
     way out of it was the close control in the corner — which is exactly the "menu you cannot
     click away" proposal 131 describes, at full size. The Home selection staying out of the URL
     is a separate half of the same finding and is deliberately not taken here. [proposals 127,
     131; §6 preamble] -->
<aside class="panel" aria-label="Title detail" use:dismiss={onClose}>
  <button class="close" onclick={onClose} aria-label="Close">✕</button>

  {#if error}
    <p class="err">{error}</p>
  {:else if !data}
    <p class="data">loading…</p>
  {:else}
    {@const t = data.title}
    <!-- §6.8's poster, beside the name rather than above it (decision 483): above it, a phone
         would push the overview and both actions below the fold. Inert, so a tap on it is a tap
         inside the panel and never reaches `dismiss`. -->
    <div class="head">
      <div class="thumb"><RatePoster title={t} showName={false} /></div>
      <div class="head-text">
        <h2>{t.name}</h2>
        <div class="data sub">{subline}</div>
        {#if t.original_name && t.original_name !== t.name}
          <div class="data">{t.original_name}</div>
        {/if}
      </div>
    </div>

    {#if t.overview}<p class="overview">{t.overview}</p>{/if}

    {#if t.trailer_key}
      <!-- §6.0 lists the trailer key as M0 content on the card, and the content is the trailer:
           the key is the link's address, not its text. It was printed as the label, and a member
           read `TRAILER F-eMt3SrfFU` (user test 2026-09-25). -->
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
      <!-- §6.0: the model line, in the data voice, never bare: `b(t) 0.52 · β 0.8 · gate 0.93`.
           Rendered from the server's own `text`, not recomposed here, so the card and §6.7's rail
           print the same number to the same precision.

           Present only while the viewer's Show the model is on: decision 486 amends decision 117,
           which had left this line ungated, and the server now omits the key with the switch off
           rather than this card hiding what it was sent. -->
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

    <!-- Decision 487: §6.1's four sweep answers, on the card of a title the person already knows.
         One group rather than four loose buttons, with the standing verdict pressed, so the row
         reads as "your answer" and a second tap is visibly a change of mind. -->
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
        <button class="btn-primary" disabled>Play on Jellyfin</button>
      {/if}
      {#if data.actions.show_on_map}
        <!-- Decision 488: absent while §6.4's Map is unbuilt, as the Map tab is. The server
             sends the target again on the day the surface ships. -->
        <a class="btn-ghost" href="/map?title={t.id}">Show on map</a>
      {/if}
    </div>
    {#if !data.actions.play_on_jellyfin}
      <!-- §6.8: every conflict carries its one-line why. This one was carried in `title=`, which
           is a hover tooltip and does not exist on touch — so on §6 preamble's primary form factor
           §6.0's second action was simply a dead button with no reason attached to it anywhere.
           The register is the quiet reason the rest of this card already speaks in, and the
           reason is the true one of two: a title outside the library is not a missing server. -->
      <p class="why actionwhy" data-testid="title-jellyfin-why">
        {playWhy(data.actions.play_reason ?? 'no_server')}
      </p>
    {/if}
    {#if syncNote}
      <!-- Under the row it reports on, in the display face with a margin of its own: it sat after
           the series note in the mono data voice at -4 px, and read as part of the CAST & CREW
           heading below it (user test 2026-09-25). -->
      <p class="why syncnote" role="status">{syncNote}</p>
    {/if}
    {#if t.kind === 'series' && t.seen_state === 'seen'}
      <!-- Decision 210(a): a series is app-only in the un-marking direction. Jellyfin stores no
           Played flag on a Series at all — it computes the folder's from its episodes — so the only
           way to un-mark one is a recursive DELETE across every episode, which would destroy watch
           history the app never recorded and cannot put back. The app's own state is authoritative
           either way (§7.3), and "the surface says so" is the other half of that decision: one
           quiet line in §6.8's register, where the consequence is, not a dialog in the way. -->
      <div class="data seriesnote" data-testid="title-series-unseen-note">
        Marking a series not seen is kept in Spielplan only — Jellyfin is never told to un-play its
        episodes.
      </div>
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
          <!-- Keyed by person AND role class, delimited (`creditKey`): `credits_for` collapses to
               one row per (person, role class), because one person reached the card twice when
               two sources spelled one job two ways - Heat's composer as "Original Music Composer"
               and "Composer" (user test 2026-09-25). The job is the key's fallback for a payload
               without the class. The delimiter is what stops person 700 + `1Actor` colliding
               with person 7001 + `Actor`; the undelimited key threw on 1,216 real titles where one
               person held one job under two department spellings, and with no +error.svelte the
               whole card died mid-render. Keyed, not unkeyed, because the key is what keeps
               `onPerson` attached to the right person. [C9.3 of the 2026-09-25 user test]

               The job line names further jobs only where they are different credits (Writer ·
               Novel), never a second spelling; the source count is provenance for the operator
               and rides on Show the model (decision 486). -->
          {#each shownCredits as c (creditKey(c))}
            <button class="person" onclick={() => onPerson(c)}>
              <span class="dot">{c.name.charAt(0)}</span>
              <span class="pname">{c.name}</span>
              <span class="data"
                >{[
                  creditJobs(c),
                  showModel && c.sources?.length > 1 ? `${c.sources.length} sources` : null
                ]
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

    <!-- §4.1 rule 1: the two tiers are visibly distinct, and never interleaved. The distinction
         is the rule; the words were the operator's ("DNA — EXTRACTED quote-verified"), and the
         headings now say what each tier is to someone choosing a film (decision 486). -->
    <section>
      <div class="data heading">WHAT IT'S LIKE <span class="qv">each one quoted</span></div>
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

             The term by its LABEL (decision 486 clause 4): `era.wwii` is the key and "World War
             II" is the term. Printing the facet on top of the id read
             `narrative_themes.themes.love_romance` [M4.9 finding 3]; printing the id alone read
             `register.plays_it_straight` to a member (user test 2026-09-25). The facet is spent
             on the colour, which is the identity §6.8 asks for, and the id itself appears only
             beside the label while Show the model is on. -->
        {#each data.dna.extracted as tag (tag.facet + ':' + tag.term + ':' + tag.provider)}
          <div class="tag" style:border-left-color={facetColour(tag.facet)}>
            <div class="tagline">
              <span class="term" style:color={facetColour(tag.facet)} title={tag.gloss ?? undefined}
                >{termLabel(tag)}</span
              >
              {#if showModel}
                <span class="data">{[tag.term, tag.salience != null ? `sal ${tag.salience}` : null]
                    .filter(Boolean)
                    .join(' · ')}</span>
              {/if}
            </div>
            {#each tag.evidence as e}
              <!-- A span cut mid-sentence is marked as a fragment; the stored quote is what §4.1
                   rule 1 verified and it is untouched. `lib/quote.js` says why. [C9.6 of the
                   2026-09-25 user test] -->
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
      {#if data.dna.projected.length}
        <div class="chips">
          <!-- Same label rule as the extracted tier above, and one key component fewer:
               `dna_projected` is UNIQUE (title_id, version, term), so no second provider can put
               one term on this list twice and the term is a key here on its own merits.
               [M4.9 review cycle 1]

               Each chip carries its weight, which is how many sources suggested it, and a
               one-source chip is drawn fainter: Heat's lone "teenage girl" tag made
               "teen protagonist" look as settled as a four-source "Los Angeles" (user test
               2026-09-25, C9.5). Emphasis only, never a filter - §4.1 rule 2: weights are never
               filters, so no chip is dropped however weak. -->
          {#each data.dna.projected as p (p.facet + ':' + p.term)}
            {@const n = p.weight == null ? null : Math.round(p.weight)}
            <span
              class="chip"
              class:faint={n != null && n <= 1}
              style:color={facetColour(p.facet)}
              style:border-color={facetColour(p.facet)}
              title={[p.gloss, n != null ? `suggested by ${n} source${n === 1 ? '' : 's'}` : null]
                .filter(Boolean)
                .join(' - ')}
              data-weight={n}
            >
              <span class="chiplabel">{termLabel(p)}</span>
              {#if n != null}<span class="n" aria-label={`${n} source${n === 1 ? '' : 's'}`}>{n}</span>{/if}
              {#if showModel}<span class="rawid">{p.term}</span>{/if}
            </span>
          {/each}
        </div>
      {:else}
        <p class="why">Nothing inferred for this title yet.</p>
      {/if}
    </section>
  {/if}
</aside>

<style>
  /* §6 preamble makes this an installable PWA, and `app.html` asks for `viewport-fit=cover` with
     a black-translucent status bar: the installed web view starts UNDER the status bar, so a
     panel offset by a bare 54 px starts that many pixels too high and its first rows sit behind
     the clock. The header is sized `calc(54px + env(safe-area-inset-top))`, and anything anchored
     below it has to say the same thing rather than a number that was only ever the header's
     height on a browser tab. `env()` resolves to 0 everywhere this suite runs, which is why the
     rule is asserted at the source. [§6 preamble; decision 279] */
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
  /* `design.css`'s coarse block raises `min-height` and never `min-width`, so this control came
     out 48 px tall and 32 px wide — two thirds of §6 preamble's floor on its narrow axis, on the
     only exit a full-bleed panel has. The token is named here rather than widened globally,
     because a blanket `min-width` in the coarse block would reach every narrow control in the
     app at once. (The two casualties that argument used to name are not among them: ShelfRow's
     `.nudge` is `display: none` under `pointer: coarse`, and RateBlockCounter's ticks are
     `<span>`s inside an `aria-hidden` container, so the coarse block's six selectors miss both.
     The argument for keeping the fix scoped stands on its own; those two examples did not.)
     BEHIND `pointer: coarse`, because that is where the rule it completes lives. Declared
     unconditionally it made the control 48 wide and 32 tall on a mouse — the same lopsidedness
     rotated — and its 48 px box then started 12 px inside the heading beside it, where a 4 px gap
     had been. §6's preamble writes the floor for fingers; a mouse has no such threshold.
     [§6 preamble; M4.15 review cycle 1] */
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
    /* §6.0's trailer key is content, but the thing drawn around it is a control — a bordered,
       padded, pill-radius chip, which is the same sentence `+layout.svelte` uses to admit
       `a.nobundle` to this rule and the line that separates both from an inline prose link. It
       measured 26 tall, a little over half the floor, inside the overlay whose only OTHER exit is
       the rule above; and a bare `<a>` sits outside design.css's coarse selector list by design,
       so nothing reached it on either axis. Height is its short one: at 183 wide it clears the
       other by a factor of three. Exit criterion 4 admits exactly one exemption and names it
       (decision 280); this was a second, exempt by silence.

       `align-items` with it, because `baseline` in a box taller than its content puts both spans
       at the top of the 48 px target instead of in the middle of it.
       [§6 preamble; §6.0; review cycle 3: M415-C3-COMP-01] */
    .trailer {
      min-height: var(--touch);
      align-items: center;
    }
  }
  /* The right margin reserves the widest the close control is ever drawn, not the widest it used
     to be: `.close` sits at `right: 16px` inside the panel's 20 px padding, so on a coarse
     pointer its box starts `--touch` from the text edge and a 32 px margin put the last
     characters of a long title under a transparent hit area. [§6 preamble] */
  h2 {
    margin: 0 var(--touch) 4px 0;
    font-size: 19px;
    font-weight: 600;
  }
  .sub {
    margin-bottom: 10px;
  }
  /* The prototype's 88 x 132 poster, the name and its lines beside it. */
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
  /* Decision 487's answers: `.pill` is §6.8's selection grammar, so the standing verdict wears
     the one accent and the others do not. Wraps rather than scrolls on a narrow phone. */
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
    align-items: baseline;
    font-size: 12px;
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
  .seriesnote {
    margin-top: -4px;
    color: var(--ink-4);
  }
  /* Pulled up under the actions like `.actionwhy`, and given the section gap below it, so the
     note belongs to the row it reports on and not to the CAST & CREW heading after it. */
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
  /* Pulled up under the row it explains: `.actions` already carries the 18 px that separates it
     from the next section, and a paragraph's own margins on top of it would read as a sentence
     belonging to neither. */
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
      /* In both places, because this override wins on the form factor the finding lives on:
         a base rule carrying the inset and a phone rule replacing it with a bare 54 px is the
         inset silently discarded on the only device it is for. [decision 279] */
      top: calc(54px + env(safe-area-inset-top));
      width: 100%;
      border-left: none;
    }
  }
</style>
