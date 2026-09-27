<script>
  // Each read is its own, so a licence list that fails never blanks the importer.
  import { onMount } from 'svelte';
  import { get } from '$lib/api.js';
  import { bootstrap } from '$lib/session.svelte.js';
  import BundleImport from '$lib/components/BundleImport.svelte';

  let bundleState = $state(null);
  let error = $state('');
  let sources = $state(null);

  const HISTORY = {
    active: ['In use', 'ok'],
    staged: ['Staged', ''],
    validated: ['Checked', ''],
    superseded: ['Replaced', ''],
    failed: ['Failed', 'bad']
  };

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

{#snippet chevron()}
  <svg class="chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="m5.5 9.5 6.5 6.5 6.5-6.5" /></svg>
{/snippet}

{#snippet warning(headline)}
  <span class="warn-head">
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M12 4 2.8 19.5h18.4z" /><path d="M12 10v4.5M12 17.2v.01" />
    </svg>
    {headline}
  </span>
{/snippet}

<h1 class="large-title">Movie data</h1>

{#if error}
  <p class="err">{error}</p>
{:else if !bundleState}
  <p class="footnote">Loading…</p>
{:else}
  <div class="page">
    <section class="group">
      <h2 class="list-header">In use</h2>
      {#if bundleState.active}
        <div class="list-group bundle-active">
          <div class="list-row">
            <span>Version</span>
            <span class="value data-lg" data-testid="bundle-version">{bundleState.active}</span>
          </div>
          {#if bundleState.loaded?.titles}
            <div class="list-row">
              <span>Titles</span>
              <span class="value data-lg">{bundleState.loaded.titles.toLocaleString()}</span>
            </div>
          {/if}
          {#if bundleState.loaded?.vocabulary_version}
            <div class="list-row">
              <span>Vocabulary</span>
              <span class="value data-lg">{bundleState.loaded.vocabulary_version}</span>
            </div>
          {/if}
        </div>
      {:else}
        <p class="card why">
          No movie data is loaded yet. The app still runs: the admin pages work, and the screens
          that need movie data say so until you import it.
        </p>
      {/if}

      {#if bundleState.restart_required}
        <!-- The backend loads a flipped bundle by itself, so reaching this means that load failed. -->
        <div class="card warn">
          {@render warning('Restart needed')}
          <p>
            This server still uses {bundleState.loaded?.version ?? 'nothing'}: it could not load
            {bundleState.active} by itself, and its log says why. Once that is fixed,
            restart backend and worker to load it:
          </p>
          <code class="code">docker compose restart backend worker</code>
        </div>
      {/if}
      {#if bundleState.broken}
        <!-- The server's `restart_required` excludes `broken`, so the two never render together. -->
        <div class="card warn">
          {@render warning('Movie data files are missing')}
          <p>
            The files for {bundleState.active} are gone, so the taste jobs pause rather than run on
            nothing. Restore /data/artifacts from backup and restart backend and worker, or
            import {bundleState.active} again: a re-import of the version in use restages its files
            and rebuilds what depends on them.
          </p>
          <code class="code">{bundleState.missing_path}</code>
        </div>
      {/if}
      {#if bundleState.loaded?.missing_required?.length}
        <div class="card warn">
          {@render warning('Some required files are missing')}
          <code class="code">{bundleState.loaded.missing_required.join(', ')}</code>
        </div>
      {/if}
    </section>

    <section class="group">
      <h2 class="list-header">Import</h2>
      <BundleImport
        importJob={bundleState.import_job}
        onImported={async () => {
          await refresh();
          await bootstrap();
        }}
      />
    </section>

    {#if bundleState.bundles.length}
      <section class="group">
        <h2 class="list-header">History</h2>
        <div class="list-group">
          {#each bundleState.bundles as b (b.version)}
            {@const [word, tone] = HISTORY[b.state] ?? [b.state, '']}
            <div class="list-row">
              <span class="text">
                <span>{b.version}</span>
                <span class="footnote">{new Date(b.imported_at).toLocaleString()}</span>
              </span>
              <span class="badge {tone}">{word}</span>
            </div>
          {/each}
        </div>
      </section>
    {/if}

    <section class="group">
      <h2 class="list-header">Reference</h2>
      <div class="list-group">
        <details class="ref">
          <summary class="list-row">
            <span>What a re-import recomputes</span>{@render chevron()}
          </summary>
          <div class="ref-body">
            <p>
              A new import changes the basis everything is measured in, so these are rebuilt every
              time. A re-import is a planned event with a report, never a silent sync.
            </p>
            <ul class="code">
              {#each bundleState.rebuild_set as r}<li>{r}</li>{/each}
            </ul>
          </div>
        </details>

        {#if sources}
          <details class="ref terms">
            <summary class="list-row">
              <span>Sources and licences</span>{@render chevron()}
            </summary>
            <ul class="ref-body sources">
              {#each sources.sources as s (s.id)}
                <li>
                  {#if linkable(s.url)}
                    <a href={s.url} rel="noreferrer">{s.name}</a>
                  {:else}
                    <span class="source-name">{s.name}</span>
                  {/if}
                  <!-- `||`, not `??`: some datasets state no version as the empty string. -->
                  <span class="footnote">
                    {[s.license ?? 'Licence not stated', s.version || null].filter(Boolean).join(' · ')}
                  </span>
                  {#if s.notes}<p class="footnote">{s.notes}</p>{/if}
                </li>
              {/each}
            </ul>
          </details>

          {#if !sources.axes.loaded}
            <details class="ref axes">
              <summary class="list-row">
                <span>Axis files not written yet</span>{@render chevron()}
              </summary>
              <div class="ref-body">
                <p>
                  Each kind of tag can have an authored axis: a left pole, a right pole and term
                  weights. This movie data ships none, and without them:
                </p>
                <!-- The backend's sentences: what the missing file costs is the importer's claim. -->
                <ul>
                  {#each sources.axes.disables as line}<li>{line}</li>{/each}
                </ul>
                <p>
                  The importer looks for
                  {sources.axes.expected.length === 1 ? 'this file' : 'these files'} inside it:
                </p>
                <ul class="code">
                  {#each sources.axes.expected as path}<li>{path}</li>{/each}
                </ul>
              </div>
            </details>
          {/if}
        {/if}
      </div>
    </section>
  </div>
{/if}

<style>
  h1 {
    padding-top: 8px;
    margin-bottom: 16px;
  }
  .page {
    display: flex;
    flex-direction: column;
    gap: 32px;
  }
  .group {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .card.why {
    margin: 0;
  }
  .err {
    margin: 0;
    color: var(--negative);
  }
  .text {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    overflow-wrap: anywhere;
  }
  .text .footnote {
    font-variant-numeric: tabular-nums;
  }

  .warn {
    display: flex;
    flex-direction: column;
    gap: 12px;
    margin-top: 8px;
  }
  .warn p {
    margin: 0;
    font-size: var(--fs-callout);
    line-height: 21px;
    color: var(--text-2);
  }
  .warn-head {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: var(--fs-body);
    line-height: 22px;
    font-weight: 600;
  }
  .warn-head svg {
    flex: none;
    width: 22px;
    height: 22px;
    fill: none;
    stroke: var(--warning);
    stroke-width: 1.75;
    stroke-linecap: round;
    stroke-linejoin: round;
  }
  .warn .code {
    overflow-wrap: anywhere;
  }

  .ref + .ref {
    box-shadow: inset 0 0.5px 0 var(--separator);
  }
  .ref summary {
    justify-content: space-between;
    list-style: none;
    cursor: pointer;
  }
  .ref summary::-webkit-details-marker {
    display: none;
  }
  .chevron {
    flex: none;
    width: 16px;
    height: 16px;
    fill: none;
    stroke: rgba(245, 240, 232, 0.35);
    stroke-width: 1.75;
    stroke-linecap: round;
    stroke-linejoin: round;
    transition: transform 0.2s var(--ease);
  }
  .ref[open] .chevron {
    transform: rotate(180deg);
  }
  .ref-body {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin: 0;
    padding: 0 var(--gutter) var(--gutter);
    font-size: var(--fs-subhead);
    line-height: 20px;
    color: var(--text-2);
  }
  .ref-body p {
    margin: 0;
  }
  .ref-body ul {
    margin: 0;
    padding-left: 18px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .ref-body ul.code {
    overflow-wrap: anywhere;
  }
  .sources {
    list-style: none;
  }
  ul.sources {
    padding-left: var(--gutter);
    gap: 12px;
  }
  .sources li {
    display: flex;
    flex-direction: column;
  }
  .sources .footnote {
    margin: 0;
  }
  /* A bare link is no 48px target by itself. */
  .sources a,
  .source-name {
    display: inline-flex;
    align-items: center;
    min-height: 44px;
    color: var(--text);
    font-size: var(--fs-body);
  }
  .sources a {
    color: var(--accent-text);
  }

  @media (pointer: coarse) {
    .sources a {
      min-height: var(--touch);
    }
  }
</style>
