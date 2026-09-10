<script>
  /**
   * Admin → Data. Spec v2.1 §6.6: "artifact-bundle import wizard (validate → report →
   * hot-swap; §10), acquisition pipeline monitor, extraction queue, review of DNA rejects".
   * §3.1 scopes this page to M0 — it is the same importer the first-boot wizard runs.
   * The acquisition board and the flywheel arrive with M5 and say so.
   */
  import { onMount } from 'svelte';
  import { get } from '$lib/api.js';
  import { bootstrap } from '$lib/session.svelte.js';
  import AdminTabs from '$lib/components/AdminTabs.svelte';
  import BundleImport from '$lib/components/BundleImport.svelte';

  let state = $state(null);
  let error = $state('');
  // Two read-only lists §6.6 owes an operator and this page could not previously show: the
  // per-dataset licence terms the loader dropped at the boundary until M4.9, and §6.4's axis
  // artifact, which nobody has authored. Both are facts about the imported corpus, which is
  // what this card is for. Fetched separately from `/admin/bundle/state` so a failure to read
  // either one cannot take the import wizard down with it.
  let sources = $state(null);

  // Joined in JS: Svelte collapses the whitespace around {#if} blocks, which ate the
  // separators and rendered "test-v1· vocabulary v1".
  const activeLine = $derived(
    state?.active
      ? [
          `active: ${state.active}`,
          state.loaded ? `vocabulary ${state.loaded.vocabulary_version ?? '—'}` : null,
          state.loaded?.titles ? `${state.loaded.titles.toLocaleString()} titles` : null
        ]
          .filter(Boolean)
          .join(' · ')
      : ''
  );

  // `rating_source.url` arrives from the bundle, which is operator-supplied data rather than
  // this app's own string. §14's posture is that a bundle is trusted to be loaded, not trusted
  // to be rendered as an href: a `javascript:` value would run in the admin's session on the one
  // page an admin is certain to visit. The name still prints — the row is the point — it just
  // stops being a link.
  const linkable = (url) => typeof url === 'string' && /^https?:\/\//i.test(url);

  async function refresh() {
    try {
      state = await get('/admin/bundle/state');
    } catch (err) {
      error = err.message;
    }
    try {
      sources = await get('/admin/data/sources');
    } catch {
      // Deliberately silent: the wizard above is this page's job and the two lists below are
      // reference material. A licence list that failed to load must not read as an import that
      // failed.
      sources = null;
    }
  }

  onMount(refresh);
</script>

<AdminTabs active="data" />

<h1>Artifact bundle</h1>

{#if error}
  <p class="err">{error}</p>
{:else if state}
  {#if state.active}
    <div class="bundle-active card">
      <div class="data-lg">{activeLine}</div>
      {#if state.restart_required}
        <!-- §10: the swap sequence ends in a restart. Until it happens the flip is real in
             the database and invisible to this process, and saying so is the difference
             between "it worked" and "did it work?".
             The command, not the instruction: the importer performs no restart, so this banner
             is where an operator learns what to type, and it is the same string README's
             Recovery section gives so the two cannot drift. [M4.7 ops-09, ds10] -->
        <div class="warn data">
          loaded in this process: {state.loaded?.version ?? 'none'} — restart backend and worker
          to load {state.active}: docker compose restart backend worker
        </div>
      {/if}
      {#if state.loaded?.missing_required?.length}
        <div class="warn data">missing required: {state.loaded.missing_required.join(', ')}</div>
      {/if}
    </div>
  {:else}
    <p class="why">
      No bundle is active. That is a legal state — the app runs, the wizard and admin routes
      work, and artifact-dependent surfaces render their no-bundle state.
    </p>
  {/if}

  <BundleImport
    onImported={async () => {
      await refresh();
      await bootstrap();
    }}
  />

  {#if state.bundles.length}
    <h2>History</h2>
    <table>
      <thead>
        <tr><th class="data">VERSION</th><th class="data">STATE</th><th class="data">IMPORTED</th></tr>
      </thead>
      <tbody>
        {#each state.bundles as b (b.version)}
          <tr>
            <td class="data-lg">{b.version}</td>
            <td class="data" class:active={b.state === 'active'}>{b.state}</td>
            <td class="data">{new Date(b.imported_at).toLocaleString()}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}

  <section class="rebuild">
    <div class="data heading">RECOMPUTED ON EVERY RE-IMPORT</div>
    <ul>
      {#each state.rebuild_set as r}<li class="why">{r}</li>{/each}
    </ul>
    <p class="why">
      Everything expressed in the old Backbone’s basis is garbage against a new one, so a
      re-import is a planned event with a diff report — never a silent sync.
    </p>
  </section>

  {#if sources}
    <!-- §4.1 rule 4's eleven frozen ids, with the terms each dataset ships. The licence column
         is the one an operator has to read before sharing an archive, so it is a column and not
         a tooltip. -->
    <section class="terms">
      <div class="data heading">SOURCES AND TERMS</div>
      <table>
        <thead>
          <tr>
            <th class="data">SOURCE</th><th class="data">LICENCE</th><th class="data">VERSION</th>
          </tr>
        </thead>
        <tbody>
          {#each sources.sources as s (s.id)}
            <tr>
              <td class="data-lg">
                {#if linkable(s.url)}<a href={s.url} rel="noreferrer">{s.name}</a>{:else}{s.name}{/if}
              </td>
              <td class="why">{s.license ?? 'not stated'}</td>
              <!-- `||` and not `??`: four of the eleven frozen ids state no version with the
                   EMPTY STRING rather than with a NULL (v20260828: tmdb-users,
                   metacritic-users, metacritic-critics, trakt-comments), and the mapping
                   deliberately stores what the bundle said. `??` let those through as a blank
                   cell, which reads as a column the importer dropped rather than as a dataset
                   that publishes no version. [M4.9 review cycle 1: M49-MIG-04] -->
              <td class="data">{s.version || '—'}</td>
            </tr>
            {#if s.notes}
              <tr><td class="note why" colspan="3">{s.notes}</td></tr>
            {/if}
          {/each}
        </tbody>
      </table>
    </section>

    {#if !sources.axes.loaded}
      <!-- Decision 191: the loader stays as it is and the missing artifact becomes a task an
           operator can see, rather than a warning inside an import report nobody re-reads. -->
      <section class="axes">
        <div class="data heading">OUTSTANDING: AUTHOR THE AXIS ARTIFACT</div>
        <p class="why">
          §6.4 gives each vocabulary facet an authored axis — left pole, right pole, term
          weights — and the bundle ships none, so the Map has no axes to plot. The importer
          reads these {sources.axes.expected.length} paths inside the bundle:
        </p>
        <ul>
          {#each sources.axes.expected as path}<li class="data">{path}</li>{/each}
        </ul>
      </section>
    {/if}
  {/if}
{:else}
  <p class="data">loading…</p>
{/if}

<style>
  h1 {
    margin: 0 0 12px;
    font-size: 19px;
    font-weight: 600;
  }
  h2 {
    margin: 24px 0 8px;
    font-size: 15px;
    font-weight: 600;
  }
  .bundle-active {
    /* A one-line status band above the import controls, not a card the eye rests in. */
    padding: var(--card-pad-tight);
    margin-bottom: 14px;
  }
  .warn {
    color: var(--ember-lift);
    margin-top: 4px;
  }
  table {
    width: 100%;
    border-collapse: collapse;
  }
  th {
    text-align: left;
    padding: 6px 8px;
    border-bottom: 1px solid var(--line);
    letter-spacing: 0.1em;
  }
  td {
    padding: 8px;
    border-bottom: 1px solid var(--line);
  }
  td.active {
    color: #5fae7a;
  }
  .rebuild,
  .terms,
  .axes {
    margin-top: 26px;
    padding-top: 14px;
    border-top: 1px solid var(--line);
  }
  /* 48px minimum touch target: the source name is the only tappable thing added here, and on
     the phone form factor a table cell's own padding does not reach it. */
  .terms a {
    display: inline-flex;
    align-items: center;
    min-height: 48px;
    color: inherit;
  }
  .terms td.note {
    padding-top: 0;
    border-bottom: 1px solid var(--line);
  }
  .heading {
    letter-spacing: 0.12em;
    margin-bottom: 8px;
  }
  ul {
    margin: 0 0 8px;
    padding-left: 18px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .err {
    color: var(--ember-lift);
  }
</style>
