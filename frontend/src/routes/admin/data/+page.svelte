<script>
  // Each card below the importer fetches its own reads, so one that fails never blanks this page.
  import { onMount } from 'svelte';
  import { get } from '$lib/api.js';
  import { bootstrap } from '$lib/session.svelte.js';
  import AdminTabs from '$lib/components/AdminTabs.svelte';
  import BundleImport from '$lib/components/BundleImport.svelte';
  import AcquisitionBoard from '$lib/components/AcquisitionBoard.svelte';
  import FlywheelQueue from '$lib/components/FlywheelQueue.svelte';
  import DnaRejects from '$lib/components/DnaRejects.svelte';
  import LedgerEditor from '$lib/components/LedgerEditor.svelte';

  let bundleState = $state(null);
  let error = $state('');
  // Fetched apart from `/admin/bundle/state` so a failure here cannot take the import wizard down.
  let sources = $state(null);

  // Joined in JS: Svelte collapses the whitespace around {#if} blocks, eating the separators.
  const activeLine = $derived(
    bundleState?.active
      ? [
          `active: ${bundleState.active}`,
          bundleState.loaded ? `vocabulary ${bundleState.loaded.vocabulary_version ?? '—'}` : null,
          bundleState.loaded?.titles ? `${bundleState.loaded.titles.toLocaleString()} titles` : null
        ]
          .filter(Boolean)
          .join(' · ')
      : ''
  );

  // A bundle is trusted to load, not to render as an href: a `javascript:` URL would run here.
  const linkable = (url) => typeof url === 'string' && /^https?:\/\//i.test(url);

  async function refresh() {
    try {
      bundleState = await get('/admin/bundle/state');
    } catch (err) {
      error = err.message;
    }
    try {
      sources = await get('/admin/data/sources');
    } catch {
      // Silent: a licence list that failed to load must not read as a failed import.
      sources = null;
    }
  }

  onMount(refresh);
</script>

<AdminTabs active="data" />

<h1>Artifact bundle</h1>

{#if error}
  <p class="err">{error}</p>
{:else if bundleState}
  {#if bundleState.active}
    <div class="bundle-active card">
      <div class="data-lg">{activeLine}</div>
      {#if bundleState.restart_required}
        <!-- The backend loads a flipped bundle by itself, so reaching this means that load failed. -->
        <div class="warn data">
          loaded in this process: {bundleState.loaded?.version ?? 'none'} — the backend could not
          load {bundleState.active} by itself (its log says why); once that is fixed,
          restart backend and worker to load it: docker compose restart backend worker
        </div>
      {/if}
      {#if bundleState.broken}
        <!-- The server's `restart_required` excludes `broken`, so the two banners never render together. -->
        <div class="warn data">
          bundle directory missing: {bundleState.missing_path} — the model jobs refuse rather than
          refitting in a zero basis. Restore /data/artifacts from backup and restart backend and
          worker, or import {bundleState.active} again: a re-import of the active version restages
          its files and re-runs the rebuild set.
        </div>
      {/if}
      {#if bundleState.loaded?.missing_required?.length}
        <div class="warn data">missing required: {bundleState.loaded.missing_required.join(', ')}</div>
      {/if}
    </div>
  {:else}
    <p class="why">
      No bundle is active. That is a legal state — the app runs, the wizard and admin routes
      work, and artifact-dependent surfaces render their no-bundle state.
    </p>
  {/if}

  <BundleImport
    importJob={bundleState.import_job}
    onImported={async () => {
      await refresh();
      await bootstrap();
    }}
  />

  {#if bundleState.bundles.length}
    <h2>History</h2>
    <table>
      <thead>
        <tr><th class="data">VERSION</th><th class="data">STATE</th><th class="data">IMPORTED</th></tr>
      </thead>
      <tbody>
        {#each bundleState.bundles as b (b.version)}
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
      {#each bundleState.rebuild_set as r}<li class="why">{r}</li>{/each}
    </ul>
    <p class="why">
      Everything expressed in the old Backbone’s basis is garbage against a new one, so a
      re-import is a planned event with a migration report — never a silent sync.
    </p>
  </section>

  {#if sources}
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
              <!-- `||`, not `??`: some datasets state no version as the empty string. -->
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
      <section class="axes">
        <div class="data heading">OUTSTANDING: AUTHOR THE AXIS ARTIFACT</div>
        <p class="why">
          §6.4 gives each vocabulary facet an authored axis — left pole, right pole, term
          weights — and the bundle ships none. Without them:
        </p>
        <!-- The sentences are the backend's: what the missing artifact costs is the importer's claim. -->
        <ul>
          {#each sources.axes.disables as line}<li class="why">{line}</li>{/each}
        </ul>
        <p class="why">
          The importer reads these {sources.axes.expected.length} paths inside the bundle:
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

<section aria-label="Acquisition board">
  <AcquisitionBoard />
</section>

<section aria-label="Extraction flywheel">
  <FlywheelQueue />
</section>

<section aria-label="DNA review">
  <DnaRejects />
</section>

<section aria-label="Ledger editors">
  <h2>Ledger editors</h2>
  <LedgerEditor ledger="adjudications" />
  <LedgerEditor ledger="corrections" />
  <LedgerEditor ledger="axes" />
</section>

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
  /* A table cell's padding does not make the source link a 48px target. */
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
