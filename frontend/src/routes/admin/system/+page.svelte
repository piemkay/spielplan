<script>
  /**
   * Admin → System. Spec v2.1 §6.6, §2 (Backups, Configuration), §14.3; decision 182.
   *
   * §6.6 names five things for this card — "job health, queue depth, last syncs, backup
   * status, logs" — and §12 puts §6.6's admin surfaces at M5. Decision 182 ships three of
   * them now and leaves the rest there, because M4.7 is the milestone that gives `job_run`
   * and secrets custody a readable state and it would otherwise ship them with no reader:
   * "did last night's dump happen" would still be a question only psql can answer.
   *
   * READ-ONLY, deliberately. Rotation and repair are `spielplan-secrets` and the dump is the
   * worker's; §2 makes rotation "an explicit admin action" by an operator, and a card that
   * could start either would already be the M5 surface this one is not.
   *
   * The key itself never appears. §14.3: a Jellyfin API key is unscoped and admin-equivalent,
   * and SECRETS_KEY is what opens the stored one — so the fingerprint below is a truncated
   * digest (`core/secrets.key_fingerprint`), enough to compare this install against the `.env`
   * beside the dumps and no use for anything else.
   */
  import { onMount } from 'svelte';
  import { get } from '$lib/api.js';
  import AdminTabs from '$lib/components/AdminTabs.svelte';

  // `card`, not `state`: `let state = $state(...)` is what makes `npm run check` report
  // "Block-scoped variable '$state' used before its declaration" on the sibling Data tab, and a
  // new surface should not add a line to a list somebody has to keep reading past.
  let card = $state(null);
  let error = $state('');

  onMount(async () => {
    try {
      card = await get('/admin/system');
    } catch (err) {
      error = err.message || String(err);
    }
  });

  const stamp = (iso) => (iso ? new Date(iso).toLocaleString() : null);

  /**
   * How long ago, in the coarsest unit that is still true.
   *
   * The age is the fact, not the timestamp: §2 promises one dump a night, so "19 hours ago"
   * answers the question and "2026-09-06 03:04:12" makes the reader do the subtraction.
   */
  function ago(iso) {
    if (!iso) return '';
    const minutes = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
    if (minutes < 60) return `${minutes} minute${minutes === 1 ? '' : 's'} ago`;
    const hours = Math.round(minutes / 60);
    if (hours < 48) return `${hours} hour${hours === 1 ? '' : 's'} ago`;
    const days = Math.round(hours / 24);
    return `${days} days ago`;
  }

  const megabytes = (bytes) =>
    typeof bytes === 'number' ? `${(bytes / 1_000_000).toFixed(1)} MB` : null;

  /**
   * A job's outcome in one word. `finished_at === null` is its own answer and not a missing
   * one: the worker opens the row before the job runs, so a job killed mid-flight — a SIGKILL
   * past the grace period, an OOM, a power cut — leaves exactly this, which is a more useful
   * fact than no row.
   */
  const outcome = (job) => (job.finished_at === null ? 'unfinished' : job.ok ? 'ok' : 'failed');

  /**
   * The row's own `detail`, which is whatever the job's report produced — the failure text on
   * a failure, the report's numbers otherwise. Flattened to its scalar entries: a report that
   * grows a nested field must not be able to make this page render `[object Object]`.
   */
  function detail(job) {
    const d = job.detail;
    if (!d || typeof d !== 'object') return '';
    if (typeof d.error === 'string') return d.error;
    return Object.entries(d)
      .filter(([, v]) => v === null || ['string', 'number', 'boolean'].includes(typeof v))
      .slice(0, 4)
      .map(([k, v]) => `${k} ${v}`)
      .join(' · ');
  }
</script>

<AdminTabs active="system" />

<h1>System</h1>
<p class="why">
  Three facts, read-only: whether last night's dump happened, which SECRETS_KEY this process
  holds, and how each background job last ended. Job health in full, queue depth, last syncs
  and logs are §6.6's too and arrive with M5.
</p>

{#if error}
  <p class="err" role="alert">{error}</p>
{:else if !card}
  <p class="data">loading…</p>
{:else}
  <!-- Fact 1. §2: "nightly pg_dump to /data/backups, rotation 14". The newest *successful*
       dump, not the newest attempt — on the install that needs this page they are different
       rows, and the newest attempt is the one that says nothing. -->
  <section class="card fact" data-testid="system-backup">
    <h2>Last successful backup</h2>
    {#if card.backup.at}
      <div class="data-lg">
        {stamp(card.backup.at)} · {ago(card.backup.at)}{megabytes(card.backup.bytes)
          ? ` · ${megabytes(card.backup.bytes)}`
          : ''}
      </div>
    {:else}
      <div class="data-lg">no dump has ever completed on this install</div>
    {/if}
    {#if card.backup.stale}
      <!-- A household that has never completed a dump is stale by the same rule, and that is
           the point: "no backup yet" and "no backup since Tuesday" are one problem to the
           person who needs one. -->
      <p class="warn" data-testid="system-backup-stale">
        No successful dump in the last {card.backup.stale_after_hours} hours. Check the
        nightly-backup row below, then <code class="data-lg">ls -l data/backups</code> — a
        <code class="data-lg">.partial</code> file is not a backup.
      </p>
    {/if}
  </section>

  <!-- Fact 2. §14.3 makes the stored connector credential admin-equivalent, so this says
       WHICH key is loaded and never what it is. The pair is the answer: the key_id names the
       row every ciphertext points at, the fingerprint names the env that has to open it. -->
  <section class="card fact" data-testid="system-secrets">
    <h2>Secrets custody</h2>
    {#if card.secrets.configured}
      <div class="data-lg">
        SECRETS_KEY fingerprint <code>{card.secrets.fingerprint}</code> · active key_id
        <code>{card.secrets.key_id ?? 'none yet'}</code>
      </div>
    {:else}
      <div class="data-lg">SECRETS_KEY is not set</div>
      <p class="why">
        A legal state (§3.1): the app runs, and every connector that would store a credential
        refuses until the key is set.
      </p>
    {/if}
    {#if card.secrets.unreadable}
      <!-- "every stored secret", not "the data-encryption key": the route answers whether any
           sealed row is unopenable, which stops being the same question the moment the
           Connectors card's repair retires the unreadable key and mints a fresh one. After that
           the active key_id above is readable while the web-push pair and any second connector
           are not, and this line is the only place that still says so. [M4.7 ops-11, dd03] -->
      <p class="warn" data-testid="system-secrets-unreadable">
        SECRETS_KEY does not open every stored secret: at least one is sealed under a key this
        install no longer holds. Connector credentials stay sealed and member writes still
        commit — restore the <code class="data-lg">.env</code> that was current when the dump was
        taken, or run <code class="data-lg">spielplan-secrets reset</code> and re-enter the API
        key on Connectors.
      </p>
    {/if}
  </section>

  <!-- Fact 3. The newest job_run row per job. Before M4.7 there was no table: `last_run` was
       an in-process dict and every report was logged once and dropped, so the nightly backup
       could fail for a month with no signal a household would ever meet. -->
  <section class="fact" data-testid="system-jobs">
    <h2>Jobs</h2>
    {#if card.jobs.length === 0}
      <p class="why" data-empty="jobs">
        The worker has not recorded a run yet. On a stack that has just started this is
        expected — the first tick is seconds away, and the first nightly is tonight.
      </p>
    {:else}
      <ul class="jobs">
        {#each card.jobs as job (job.name)}
          <li data-testid="system-job" data-job={job.name} data-outcome={outcome(job)}>
            <span class="name">{job.name}</span>
            <span class="data facts">
              {outcome(job)} · {ago(job.finished_at ?? job.started_at)}
            </span>
            {#if detail(job)}<span class="data note">{detail(job)}</span>{/if}
          </li>
        {/each}
      </ul>
      <p class="why">
        The row is opened before the job runs, so “unfinished” means it started and never
        reported — a kill, an OOM or a power cut — rather than that nothing is known.
      </p>
    {/if}
  </section>
{/if}

<style>
  h1 {
    margin: 0 0 8px;
    font-size: 19px;
    font-weight: 600;
  }
  h2 {
    margin: 0 0 8px;
    font-size: 14px;
    font-weight: 600;
  }
  .fact {
    margin: 14px 0;
  }
  .warn {
    margin: 10px 0 0;
    padding: 10px 13px;
    border: 1px solid var(--ember-edge);
    background: var(--ember-wash);
    border-radius: var(--r-md);
    font-size: 13px;
  }
  .jobs {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .jobs li {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 4px 12px;
    padding: 11px 13px;
    border: 1px solid var(--line);
    border-radius: var(--r-sm);
  }
  /* A failed row is the one the page exists for, so it is the one that is not grey. */
  .jobs li[data-outcome='failed'] .facts,
  .jobs li[data-outcome='unfinished'] .facts {
    color: var(--ember-lift);
  }
  .name {
    font-size: 13.5px;
  }
  .note {
    flex-basis: 100%;
  }
  .err {
    color: var(--ember-lift);
    font-size: 12.5px;
    margin: 8px 0;
  }
</style>
