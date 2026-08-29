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
  import BundleImport from '$lib/components/BundleImport.svelte';

  let state = $state(null);
  let error = $state('');

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

  async function refresh() {
    try {
      state = await get('/admin/bundle/state');
    } catch (err) {
      error = err.message;
    }
  }

  onMount(refresh);
</script>

<div class="tabs">
  <span class="pill on">Data</span>
  <span class="pill" title="M5">Connectors</span>
  <span class="pill" title="M1">Users</span>
  <span class="pill" title="M5">System</span>
</div>

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
             between "it worked" and "did it work?". -->
        <div class="warn data">
          loaded in this process: {state.loaded?.version ?? 'none'} — restart backend and worker
          to load {state.active}
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
{:else}
  <p class="data">loading…</p>
{/if}

<style>
  .tabs {
    display: flex;
    gap: 6px;
    margin-bottom: 18px;
  }
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
    padding: 12px 14px;
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
  .rebuild {
    margin-top: 26px;
    padding-top: 14px;
    border-top: 1px solid var(--line);
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
