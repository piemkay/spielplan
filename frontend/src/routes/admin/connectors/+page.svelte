<script>
  /**
   * Admin → Connectors. Spec v2.1 §6.6, §3.3, §7, §9; decisions 339, 364, 410, 418, 450-455.
   *
   * §6.6's Connectors card in full and in its own order: Jellyfin ("URL, API key, library pick,
   * user-mapping table, test button, sync now, webhook status"), the three LLM providers with the
   * spend guard and the one task assignment M5 has a caller for (decision 339), and "TMDB / OMDb
   * / Trakt keys with test buttons".
   *
   * No stored credential is ever displayed. §14.3: "Jellyfin API keys are unscoped and
   * admin-equivalent — no read-only variant exists", and a provider key bills the household, so
   * every key field posts empty to mean "keep the stored one" and the page can only tell you
   * *whether* there is a key.
   *
   * The spend settings are never saved from a control. Each change is previewed, and only a
   * Confirm carrying the figure it was shown stores it (decision 450) -- the order lives in
   * `spendGuard.svelte.js`, which the cards below share.
   *
   * The Jellyfin card's two M5 halves each have a way to do damage in silence, and each is built
   * against it (decision 455). The library pick is its own write, sent only when it changed and
   * never from a list that failed to load, because decision 364 reads `[]` as the whole server.
   * The webhook status is the facts of what arrived and what the poll did, not a mode flag. And
   * the token is minted only by a press that asks for it, shown once and kept in this component's
   * state alone (decision 418).
   */
  import { onMount } from 'svelte';
  import { get, post, api } from '$lib/api.js';
  import { jellyfinDirectory } from '$lib/jellyfin.js';
  import { session } from '$lib/session.svelte.js';
  import { KEYLESS_LABELS, openSpendGuard, spend } from '$lib/spendGuard.svelte.js';
  import AdminTabs from '$lib/components/AdminTabs.svelte';
  import SpendMeter from '$lib/components/SpendMeter.svelte';
  import ExtractionSettings from '$lib/components/ExtractionSettings.svelte';
  import LlmProviderCard from '$lib/components/LlmProviderCard.svelte';
  import SourceConnectorCard from '$lib/components/SourceConnectorCard.svelte';

  let cfg = $state(null);
  let url = $state('');
  // Typed and not yet saved. `refresh` is async and the page's first one is still in flight when a
  // person starts typing; its late answer used to write the server's empty URL over what they had
  // typed, so Save stored the key with no URL and the card stayed unconfigured -- the unsaved
  // library pick's rule (`pickChanged` below), applied to the one text field refresh also sets.
  let urlTyped = $state(false);
  let apiKey = $state('');
  let probe = $state(null);
  let jfUsers = $state([]);
  let appUsers = $state([]);
  let syncResult = $state(null);
  let error = $state('');
  let busy = $state('');

  // Per app-user link form: the Jellyfin user to map to, plus §7.3's optional one-time
  // password entry that buys the least-privilege write path.
  let form = $state({});

  // §6.6's library pick (decision 364). `libraries` is the server's list envelope, or null while
  // unasked; `picked` is the selection on screen, which is sent only by its own button and only
  // when it differs from what is stored -- the plain Save never carries it.
  let libraries = $state(null);
  let picked = $state([]);
  let librarySeq = 0;

  // The one-time reveal (decisions 332, 418): the value the minting PUT answered, held here and
  // nowhere else -- not in storage, not in the URL -- so leaving the page is losing it.
  let minted = $state(null);
  // A press the server answered with no token while none is held: nothing was minted, and saying
  // so is the whole of it -- the press stays offered, because nothing was spent.
  let unminted = $state(false);
  // `save_jellyfin`'s own mint condition, read off the card rather than guessed at: it mints only
  // for a connector that is configured, which a key this SECRETS_KEY cannot open is not. A press
  // offered anywhere else is answered 200 with `webhook_token: null`. [M5.7 review cycle 1,
  // M57-JFSYS-02]
  const mintable = $derived(Boolean(cfg?.configured) && !cfg?.secrets_unreadable);

  const sorted = (ids) => JSON.stringify([...(ids ?? [])].sort());
  const sameIds = (a, b) => sorted(a) === sorted(b);
  const pickChanged = $derived(Boolean(cfg) && !sameIds(picked, cfg.library_ids));
  // A picked id the server no longer lists is a fault in the pick, not a boundary (decision 410):
  // only a list that loaded can say which ids those are.
  const stale = $derived(
    libraries?.ok
      ? (cfg?.library_ids ?? []).filter((id) => !libraries.libraries.some((lib) => lib.id === id))
      : []
  );
  const listed = $derived(
    libraries?.ok
      ? libraries.libraries
      : (cfg?.library_ids ?? []).map((id) => ({ id, name: id }))
  );
  // Where the plugin posts: `PUBLIC_URL` as the wizard shows it, the origin §2 serves.
  const webhookUrl = $derived(
    `${(session.publicUrl || '<PUBLIC_URL>').replace(/\/+$/, '')}/events/jellyfin`
  );

  onMount(() => {
    refresh();
    openSpendGuard();
  });

  async function refresh() {
    error = '';
    try {
      const directory = await jellyfinDirectory();
      // An unsaved pick survives a Sync or a Test; only a pick nobody touched follows the server.
      const keepPick = pickChanged;
      cfg = directory.cfg;
      if (!keepPick) picked = [...(cfg.library_ids ?? [])];
      loadLibraries(cfg.configured);
      jfUsers = directory.users;
      if (!urlTyped) url = cfg.url ?? '';
      appUsers = await get('/admin/users');
    } catch (err) {
      error = err.message;
    }
  }

  /**
   * The list to pick from. Not awaited by `refresh`: it reaches the media server on a forty-five
   * second budget, and the mapping table must not wait on it. A read that threw is the same answer
   * as one that said `ok: false` -- no list -- and the pick is disabled rather than built from it.
   */
  async function loadLibraries(configured) {
    const mine = ++librarySeq;
    if (!configured) {
      libraries = null;
      return;
    }
    let answer;
    try {
      answer = await get('/admin/connectors/jellyfin/libraries');
    } catch (err) {
      answer = { ok: false, error: err.message, libraries: [] };
    }
    if (mine === librarySeq) libraries = answer;
  }

  function pick(id, on) {
    const next = new Set(picked);
    if (on) next.add(id);
    else next.delete(id);
    // Listed order first, then the stale ids, so the stored pick reads the way the card does.
    const order = [...listed.map((lib) => lib.id), ...stale];
    picked = order.filter((each) => next.has(each));
  }

  /**
   * `library_ids` alone, through the same PUT: absent `url` and `api_key` keep what is stored. An
   * empty selection is sent as `[]` on purpose -- deselecting the last library is the one gesture
   * that widens the boundary back to the whole server (decision 364) -- but never from a list that
   * failed, where `[]` would be the shape of a choice nobody made.
   */
  async function savePick() {
    if (!libraries?.ok || !pickChanged) return;
    error = '';
    busy = 'pick';
    try {
      const answer = await api('/admin/connectors/jellyfin', {
        method: 'PUT',
        body: { library_ids: [...picked] }
      });
      // The pick as stored -- the route keeps a GUID in the server's spelling (decision 410) -- so
      // the button reads "nothing changed" against the value that is actually there.
      picked = [...(answer?.library_ids ?? picked)];
      await refresh();
    } catch (err) {
      error = err.message;
    } finally {
      busy = '';
    }
  }

  /**
   * Decision 418's explicit ask, offered only while no token exists and the connector is one the
   * server mints for. `save_jellyfin` mints only when none is held and never rotates, so a second
   * press could only ever be told "one exists".
   *
   * A `null` answer is two different facts, and only the server's next read tells them apart: a
   * token another tab minted first, which cannot be shown again, or no token at all, because the
   * connector stopped being configured between the read and the press. Reading every `null` as the
   * first told a fresh install its token was lost for good -- decision 332 allows no rotation, so an
   * admin who believed it never set up the plugin and §7.2's trigger never ran. [M5.7 review cycle
   * 1, M57-JFSYS-02]
   */
  async function mintToken() {
    error = '';
    unminted = false;
    busy = 'mint';
    try {
      const answer = await api('/admin/connectors/jellyfin', {
        method: 'PUT',
        body: { mint_webhook_token: true }
      });
      if (answer?.webhook_token) minted = { token: answer.webhook_token };
      await refresh();
      if (!minted) {
        if (cfg?.has_webhook_token) minted = { token: null };
        else unminted = true;
      }
    } catch (err) {
      error = err.message;
    } finally {
      busy = '';
    }
  }

  async function save() {
    error = '';
    busy = 'save';
    try {
      await api('/admin/connectors/jellyfin', { method: 'PUT', body: { url, api_key: apiKey } });
      apiKey = '';
      urlTyped = false;
      unminted = false;
      await refresh();
    } catch (err) {
      error = err.message;
    } finally {
      busy = '';
    }
  }

  async function test() {
    error = '';
    busy = 'test';
    probe = null;
    try {
      probe = await post('/admin/connectors/jellyfin/test');
    } catch (err) {
      error = err.message;
    } finally {
      busy = '';
    }
  }

  async function link(user) {
    error = '';
    busy = `link-${user.id}`;
    const entry = form[user.id] ?? {};
    try {
      await post(`/admin/users/${user.id}/jellyfin`, {
        jellyfin_user_id: entry.jellyfin_user_id,
        jellyfin_username: entry.username || null,
        jellyfin_password: entry.password || null
      });
      form = { ...form, [user.id]: {} };
      await refresh();
    } catch (err) {
      error = err.message;
    } finally {
      busy = '';
    }
  }

  /**
   * Complete an existing mapping with that Jellyfin user's own sign-in (§7.3's least-privilege
   * write path). Same route as Link — the mapping is unchanged, only the token is new.
   */
  async function storeSignIn(user) {
    form = {
      ...form,
      [user.id]: { ...(form[user.id] ?? {}), jellyfin_user_id: user.jellyfin_user_id }
    };
    await link(user);
  }

  async function unlink(user) {
    error = '';
    busy = `link-${user.id}`;
    try {
      await api(`/admin/users/${user.id}/jellyfin`, { method: 'DELETE' });
      await refresh();
    } catch (err) {
      error = err.message;
    } finally {
      busy = '';
    }
  }

  async function syncNow() {
    error = '';
    busy = 'sync';
    syncResult = null;
    try {
      syncResult = await post('/admin/connectors/jellyfin/sync');
      await refresh();
    } catch (err) {
      error = err.message;
    } finally {
      busy = '';
    }
  }

  function set(userId, field, value) {
    form = { ...form, [userId]: { ...(form[userId] ?? {}), [field]: value } };
  }

  const stamp = (iso) => (iso ? new Date(iso).toLocaleString() : null);

  /** The System card's rule, for the same reason: the age answers the question (`admin/system`). */
  function ago(iso) {
    if (!iso) return '';
    const minutes = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
    if (minutes < 60) return `${minutes} minute${minutes === 1 ? '' : 's'} ago`;
    const hours = Math.round(minutes / 60);
    if (hours < 48) return `${hours} hour${hours === 1 ? '' : 's'} ago`;
    const days = Math.round(hours / 24);
    return `${days} days ago`;
  }

  const when = (iso) => (iso ? `${stamp(iso)} · ${ago(iso)}` : null);

  /** The poll's newest run in one word; a run with no verdict yet is in flight or was killed. */
  function pollOutcome(poll) {
    if (!poll?.last_run_at) return 'never';
    if (poll.last_run_ok === true) return 'ok';
    if (poll.last_run_ok === false) return 'failed';
    return 'unfinished';
  }
</script>

<AdminTabs active="connectors" />

<h1>Connectors</h1>

{#if error}<p class="err" role="alert">{error}</p>{/if}

<section class="card" data-testid="connector-jellyfin">
  <h2>Jellyfin</h2>
  <p class="why">
    The API key grants full server access — Jellyfin has no read-only variant. This app uses it
    for reads and per-user Played writes only, and the Played writes go out under each linked
    person's own token.
  </p>

  {#if cfg?.secrets_unreadable}
    <!--
      Not the same message as "not configured": the connector row is there and its credentials
      are real, they are sealed under a SECRETS_KEY this install no longer holds — a restored
      dump without its env file, or a regenerated key (M4.7 dd03). Members keep working (§3.3),
      so the only person who can act on this is the admin standing in front of this card, and
      the action is Save, which re-seals under a fresh key.

      The last sentence is the cost of that shortcut, and it is here because this card is the
      only place the shortcut is offered. Saving retires the DEK it cannot open
      (`connectors/registry.save_jellyfin`), which is what lets a fresh one be minted — and
      every *other* secret sealed under the retired key stays unreadable until the original
      .env comes back. An admin who reads only "paste the key again" repairs Jellyfin and quietly
      leaves the web-push pair and any other connector behind, with the System card the only
      surface that still says so.
    -->
    <p class="alert" role="alert" data-secrets="unreadable">
      The stored credentials cannot be decrypted with this install's <code>SECRETS_KEY</code>.
      Restoring the <code>.env</code> that was current when the backup was taken recovers
      everything. Pasting the API key again below stores it under a new key and fixes Jellyfin
      only: the old key is retired, and every other secret sealed under it — web push, any other
      connector — stays unreadable until that <code>.env</code> returns. To clear those out
      instead, run <code>spielplan-secrets reset</code> and set each one up again.
    </p>
  {/if}

  <div class="grid">
    <label>
      <span class="data">SERVER URL</span>
      <input
        type="text"
        bind:value={url}
        oninput={() => (urlTyped = true)}
        placeholder="http://jellyfin.local:8096"
      />
    </label>
    <label>
      <span class="data">API KEY</span>
      <input
        type="password"
        bind:value={apiKey}
        placeholder={cfg?.has_api_key ? '•••••••• (stored)' : 'paste an API key'}
      />
    </label>
  </div>

  <div class="row">
    <button class="btn-primary" onclick={save} disabled={busy === 'save'}>Save</button>
    <button class="btn-ghost" onclick={test} disabled={!cfg?.configured || busy === 'test'}>
      {busy === 'test' ? 'Testing…' : 'Test connection'}
    </button>
    <button class="btn-ghost" onclick={syncNow} disabled={!cfg?.configured || busy === 'sync'}>
      {busy === 'sync' ? 'Syncing…' : 'Sync now'}
    </button>
  </div>

  <!-- §7.1's pin, as this install last measured it — without pressing Test. Save and Test both
       store the probed version and its verdict beside the URL now, so a 10.8 server says so every
       day rather than only in the minute after a probe. `null` is "nobody has probed yet" and must
       not read as a refusal: that is the state a fresh install is in. [M4.11 finding 16] -->
  {#if cfg?.server_version || cfg?.server_supported === false}
    <div
      class="data probe"
      data-server-supported={cfg.server_supported === null || cfg.server_supported === undefined
        ? 'unknown'
        : String(cfg.server_supported)}
    >
      server version {cfg.server_version || 'not reported'}
      {#if cfg.server_supported === false}
        · below the pinned 10.9: the per-user Played route (POST /UserPlayedItems) does not exist
        on this server, so nothing this app marks can reach Jellyfin. Reads still work.
      {:else if cfg.server_supported}· Played writes supported{/if}
    </div>
  {/if}

  {#if probe}
    <div class="data probe" data-probe={probe.ok ? 'ok' : 'fail'}>
      {#if probe.ok}
        {probe.server_name} · {probe.version} · {probe.user_count} users
        <!-- It was "reads may miss fields", which names the wrong half: by §7.1 and the client's
             own pin the 10.9-only route is the per-user Played WRITE. Reads degrade; the write does
             not exist, and an admin who read this line had no way to know the app -> Jellyfin
             direction was dead. [M4.11 finding 16] -->
        <!-- `=== false`, not `!supported`: `null` is "this server did not report a version",
             which `played_write_refusal` does not refuse on and this line must not accuse. A
             200 with no parseable version — a forward-auth portal, a hardening rule on
             /System/Info/Public — read as "below the pin" and named no version to check it
             against. [review cycle 1: m411-rev-jf-04] -->
        {#if probe.supported === false}· below the pinned 10.9 — the per-user Played write (POST
          /UserPlayedItems) does not exist here, so seen states cannot reach Jellyfin{/if}
      {:else}
        failed: {probe.error}
      {/if}
    </div>
  {/if}

  {#if syncResult?.already_running}
    <!-- §5.3 fires the sweep every fifteen minutes and this button is the other caller; the
         advisory lock answers "a sweep is already running" rather than sweeping the same people
         against two different snapshots. Pressing again once it finishes sweeps for real. -->
    <div class="data probe" data-sync="already-running">
      a sweep is already running — this press did nothing. Try again in a moment.
    </div>
  {:else if syncResult}
    <!-- `ok` is a claim about a sweep that RAN, and one state made it a claim about a sweep that
         did not. §3.3 makes an unreachable Jellyfin a degraded sync rather than a broken app, so
         `seen.sync_all` returns the report as it stands when `client.all_items(None)` raises — and
         every counter in it is zero, including `push_failed`. Printed through the rule below that
         is indistinguishable from the quiet healthy household, which is M4.11 finding 3's own
         sentence one layer up: that finding separated "owed nothing" from "lost every write" and
         left "never read the library" reading as the first. Measured: against a media server whose
         `/Items` refused the sweep's read, this card printed "pushed 0 · adopted 0 · unchanged 0"
         with `data-sync-health="ok"` while NEITHER direction of §7.3 had run.
         `users` is the signal because `sync_all` appends to it per linked member inside the loop
         the failed read returns before — and `skipped_no_link` is what separates it from the
         household that has no connector or no link at all, which is a legal §3.1 state and not a
         failure. [§7.3, §3.3, §6.6; M4.11 finding 3]

         `failed_users` is the same fault arriving by the other door, which the rule above missed:
         the library read is keyless and each member's is not, so a Jellyfin account that was
         deleted or renamed 404s that member's `/Items` for ever while the household read keeps
         succeeding. `sync_all` swallows it per member, so `users` is full, every counter is zero
         and this card printed "pushed 0 · adopted 0 · unchanged 0" in green for a sweep that
         reconciled nobody. [review cycle 1: seen-02] -->
    <div
      class="data probe"
      data-sync="done"
      data-sync-health={syncResult.push_failed
        ? 'failing'
        : !syncResult.skipped_no_link &&
            (!syncResult.users?.length || syncResult.failed_users?.length)
          ? 'unreachable'
          : 'ok'}
    >
      pushed {syncResult.pushed} · adopted {syncResult.adopted} · unchanged
      {syncResult.unchanged}
      {#if !syncResult.skipped_no_link && !syncResult.users?.length}
        <div class="alert" role="alert" data-sync-unreachable>
          this sweep never read the library, so no seen state moved in either direction — Jellyfin
          did not answer. The app keeps working and the next sweep tries again (§3.3).
        </div>
      {:else if syncResult.failed_users?.length}
        <div class="alert" role="alert" data-sync-member-failed={syncResult.failed_users.length}>
          Jellyfin answered for the library but not for {syncResult.failed_users.join(', ')}, so
          nothing was reconciled for
          {syncResult.failed_users.length === 1 ? 'that member' : 'those members'} in either
          direction. A Jellyfin account that was deleted or renamed stays like this until the
          mapping is corrected (§3.3).
        </div>
      {/if}
      {#if syncResult.needs_relink?.length}· re-link needed: {syncResult.needs_relink.join(', ')}{/if}
      {#if syncResult.owed_no_token}· {syncResult.owed_no_token} owed write(s) with no stored
        sign-in{/if}
      {#if syncResult.owed_unreachable}· {syncResult.owed_unreachable} owed write(s) for titles
        no longer in the library{/if}
      {#if syncResult.unowned}· {syncResult.unowned} title(s) no longer in the library{/if}
      <!-- A count, not a list. `SyncReport.as_dict` sends `resolve.unmatched` as `len(...)` and the
           names separately as `unmatched_names`, so `.length` on it was `undefined` and this clause
           could never render — §7.2's refused matches, which M5's acquisition pipeline consumes,
           had no surface at all. The vitest beside this file asserted it against a fabricated array,
           which is the assertion becoming the implementation compared to itself. [§7.2, §6.6] -->
      {#if syncResult.resolve?.unmatched}· {syncResult.resolve.unmatched} library
        item(s) matched no title{/if}
      <!-- D4: the names beside the count, which `SyncReport.as_dict` caps at twenty. The operator's
           only view of what the household holds that the resolver could not identify (§7.2). -->
      {#if syncResult.resolve?.unmatched_names?.length}
        <div class="unmatched" data-unmatched-names>
          {syncResult.resolve.unmatched > syncResult.resolve.unmatched_names.length
            ? `the first ${syncResult.resolve.unmatched_names.length}: `
            : ''}{syncResult.resolve.unmatched_names.join(', ')}
        </div>
      {/if}
      {#if syncResult.push_failed}
        <!-- A failure, not a count in a row of counts. This card printed
             "pushed 0 · adopted 0 · unchanged 87" while every Played write in the sweep was being
             refused, which reads as a quiet household rather than as a dead direction — the whole
             of M4.11 finding 3. The reason is the sweep's own first distinct one. -->
        <div class="alert" role="alert" data-sync-failure={syncResult.push_failed}>
          {syncResult.push_failed} Played write(s) failed — nothing reached Jellyfin for them.
          {syncResult.push_errors?.[0] ?? 'no reason was reported'}
        </div>
      {/if}
    </div>
  {/if}

  {#if cfg?.configured}
    <!-- §6.6's library pick (decisions 364, 410, 455). Checkboxes are outside design.css's coarse
         block, so each sits in a label that is the 48 px target. -->
    <div
      class="pick"
      data-library-pick={libraries === null ? 'loading' : libraries.ok ? 'ready' : 'unavailable'}
    >
      <span class="data">LIBRARY PICK</span>
      {#if libraries && !libraries.ok}
        <p class="alert" role="alert">
          The library list did not load: {libraries.error ?? 'no reason was reported'}. The pick
          stays as stored until it does, and nothing is sent from here.
        </p>
      {:else if libraries === null}
        <span class="data">asking Jellyfin for its libraries…</span>
      {/if}
      {#each listed as lib (lib.id)}
        <label class="check">
          <input
            type="checkbox"
            checked={picked.includes(lib.id)}
            disabled={!libraries?.ok || busy === 'pick'}
            onchange={(e) => pick(lib.id, e.currentTarget.checked)}
          />
          <span>{lib.name || lib.id}</span>
        </label>
      {/each}
      {#if stale.length}
        <div class="stale" data-library-stale>
          <p class="alert">
            Picked but no longer listed by the server: {stale.join(', ')}. Adds that no listed
            picked library holds are held until the pick is saved again (decision 410).
          </p>
          {#each stale as id (id)}
            <label class="check">
              <input
                type="checkbox"
                checked={picked.includes(id)}
                disabled={busy === 'pick'}
                onchange={(e) => pick(id, e.currentTarget.checked)}
              />
              <span>{id} (no longer listed)</span>
            </label>
          {/each}
        </div>
      {/if}
      <p class="why">
        {#if picked.length === 0}
          Nothing picked: every library on the server is acquired (decision 364).
        {:else}
          Only what is added to the picked libraries is acquired; an add anywhere else is recorded
          and left alone (decision 364).
        {/if}
      </p>
      <div class="row">
        <button
          class="btn-ghost"
          onclick={savePick}
          disabled={!libraries?.ok || !pickChanged || busy === 'pick'}
        >
          Save library pick
        </button>
      </div>
    </div>
  {/if}

  <!-- §6.6's "webhook status" as the facts of both paths (decision 455, proposal 106): §7.2 runs
       the webhook and the delta poll at once by design, so there is no mode to report, only what
       each last did. The last ItemAdded is not the last delivery: a plugin pointed at the playback
       templates delivers every evening and has never told this app about an add. Read with `?.`,
       because a card served without `trigger` (an older backend, 18-system's fixture) is legal. -->
  {#if cfg?.trigger}
    {@const webhook = cfg.trigger.webhook ?? {}}
    {@const poll = cfg.trigger.delta_poll ?? {}}
    <div
      class="data probe"
      data-trigger="webhook"
      data-item-added={webhook.last_item_added_at ? 'received' : 'none'}
    >
      webhook · last ItemAdded {when(webhook.last_item_added_at) ?? 'none received yet'}
      · last delivery of any kind {when(webhook.last_delivery_at) ?? 'none'} ·
      {webhook.deliveries_7d ?? 0} in the last 7 days
      {#if webhook.last_refusal}
        <div>newest refusal {when(webhook.last_refusal.at)}: {webhook.last_refusal.reason}</div>
      {/if}
    </div>
    <div class="data probe" data-trigger="delta-poll" data-poll-outcome={pollOutcome(poll)}>
      delta poll · newest run {when(poll.last_run_at) ?? 'never'} · {pollOutcome(poll)}
      {#if poll.last_filed !== null && poll.last_filed !== undefined}· filed {poll.last_filed}{/if}
      · last ok {when(poll.last_ok_at) ?? 'never'} · reads from {stamp(poll.watermark) ?? 'the start'}
    </div>
  {/if}

  <!-- §7.2's token (decisions 332, 418, 455): minted only by the press below, shown once, never
       rotated. The header and the path are shown beside it because the plugin needs all three. -->
  {#if minted}
    <div class="token" data-webhook-token-state="revealed">
      {#if minted.token}
        <p class="alert" role="alert">
          Copy this into the Jellyfin Webhook plugin now: it is shown once, here, and never again.
        </p>
        <code class="data-lg" data-webhook-token>{minted.token}</code>
      {:else}
        <p class="why">
          A token already existed, so none was minted, and it cannot be shown again.
        </p>
      {/if}
      <div class="data">
        header X-Spielplan-Token · POST {webhookUrl} · template ops/jellyfin-webhook-template.json
      </div>
    </div>
  {:else if cfg?.has_webhook_token === false}
    <!-- The press is offered only where the server would mint (M57-JFSYS-02). The card lays the
         URL and the key out above the token, so on first setup this block is reached before they
         are saved, and it says what it is waiting for rather than offering a press that mints
         nothing. -->
    <div class="token" data-webhook-token-state={mintable ? 'none' : 'unconfigured'}>
      {#if unminted}
        <p class="alert" role="alert" data-webhook-unminted>
          Nothing was minted, and no token is stored: the server mints one only for a connector
          saved with its URL and an API key it can read.
        </p>
      {/if}
      <p class="why">
        {#if mintable}
          No webhook token yet, so the Webhook plugin cannot deliver and the delta poll carries every
          add. Generating one shows it once; nothing can show it again.
        {:else}
          No webhook token yet. One can be generated once the server URL and API key are saved: the
          server mints it only for a configured connector, and shows it once.
        {/if}
      </p>
      <div class="data">
        header X-Spielplan-Token · POST {webhookUrl} · template ops/jellyfin-webhook-template.json
      </div>
      {#if mintable}
        <div class="row">
          <button class="btn-ghost" onclick={mintToken} disabled={busy === 'mint'}>
            Generate webhook token
          </button>
        </div>
      {/if}
    </div>
  {:else if cfg?.has_webhook_token}
    <p class="why" data-webhook-token-state="exists">
      A webhook token exists. It was shown once, when it was generated, and cannot be shown again;
      the delta poll carries every add without it.
    </p>
  {/if}
</section>

<section class="card">
  <h2>User mapping</h2>
  <p class="why">
    Optional and one-to-one. A link adds two-way watched state; signing in as that Jellyfin
    user once stores their own token so Played writes never use the admin key.
  </p>

  <table>
    <thead>
      <tr><th>Account</th><th>Jellyfin user</th><th>Sign-in</th><th></th></tr>
    </thead>
    <tbody>
      {#each appUsers as u (u.id)}
        <tr data-user={u.name}>
          <td>
            <div>{u.name}</div>
            <div class="data">{u.role}</div>
          </td>
          <td>
            {#if u.jellyfin_user_id}
              <div class="data" data-link-state={u.jellyfin_link_state}>
                {jfUsers.find((j) => j.id === u.jellyfin_user_id)?.name ?? u.jellyfin_user_id}
                {u.jellyfin_link_state === 'needs_relink' ? '· needs sign-in' : '· linked'}
              </div>
            {:else}
              <select
                value={form[u.id]?.jellyfin_user_id ?? ''}
                onchange={(e) => set(u.id, 'jellyfin_user_id', e.currentTarget.value)}
                aria-label={`Jellyfin user for ${u.name}`}
              >
                <option value="">not linked</option>
                {#each jfUsers as j (j.id)}<option value={j.id}>{j.name}</option>{/each}
              </select>
            {/if}
          </td>
          <td>
            {#if !u.has_jellyfin_token}
              <div class="creds">
                <input
                  type="text"
                  placeholder="jellyfin username"
                  value={form[u.id]?.username ?? ''}
                  oninput={(e) => set(u.id, 'username', e.currentTarget.value)}
                />
                <input
                  type="password"
                  placeholder="password (once)"
                  value={form[u.id]?.password ?? ''}
                  oninput={(e) => set(u.id, 'password', e.currentTarget.value)}
                />
              </div>
            {:else}
              <span class="data">token stored</span>
            {/if}
          </td>
          <td class="actions">
            {#if u.jellyfin_user_id}
              <!-- A linked row with no stored token is §7.3's needs_relink state, and the
                   credential inputs beside it are useless without something that posts them.
                   The mapping is already chosen, so this only completes it. -->
              {#if !u.has_jellyfin_token}
                <button
                  class="btn-primary"
                  onclick={() => storeSignIn(u)}
                  disabled={!form[u.id]?.username ||
                    !form[u.id]?.password ||
                    busy === `link-${u.id}`}
                >
                  Store sign-in
                </button>
              {/if}
              <button class="btn-ghost" onclick={() => unlink(u)} disabled={busy === `link-${u.id}`}>
                Unlink
              </button>
            {:else}
              <button
                class="btn-primary"
                onclick={() => link(u)}
                disabled={!form[u.id]?.jellyfin_user_id || busy === `link-${u.id}`}
              >
                Link
              </button>
            {/if}
          </td>
        </tr>
      {/each}
    </tbody>
  </table>
</section>

<SpendMeter />
<ExtractionSettings />
{#each spend.llm?.providers ?? [] as card (card.name)}
  <LlmProviderCard {card} />
{/each}

{#if spend.sourcesError}<p class="err" role="alert">{spend.sourcesError}</p>{/if}
{#each spend.sources?.sources ?? [] as source (source.name)}
  <SourceConnectorCard {source} />
{/each}
{#if spend.sources?.keyless?.length}
  <!-- Proposal 137's point (plan C2): which of stage 2's sources need a card at all, so a failure
       from one that needs none is not chased on this page. -->
  <p class="why" data-keyless>
    {spend.sources.keyless.map((name) => KEYLESS_LABELS[name] ?? name).join(', ')} need no key: a
    stage-2 failure from one of them is not fixed on this page.
  </p>
{/if}

<style>
  h1 {
    margin: 0 0 12px;
    font-size: 19px;
    font-weight: 600;
  }
  h2 {
    margin: 0 0 6px;
    font-size: 15px;
    font-weight: 600;
  }
  .card {
    margin-bottom: 16px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
  }
  label {
    display: flex;
    flex-direction: column;
    gap: 5px;
  }
  .row {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  .probe {
    padding: 7px 10px;
    border: 1px solid var(--line);
    border-radius: var(--r-sm);
  }
  .pick {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .stale {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .check {
    flex-direction: row;
    align-items: center;
    gap: 10px;
    min-height: var(--touch);
    min-width: var(--touch);
    cursor: pointer;
    font-size: 13.5px;
  }
  .check input {
    width: 18px;
    height: 18px;
    margin: 0;
  }
  .check input:disabled + span {
    color: var(--ink-4);
  }
  .token {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .token code {
    word-break: break-all;
  }
  .unmatched {
    margin-top: 4px;
  }
  .alert {
    margin: 0;
    padding: 8px 10px;
    border: 1px solid var(--ember-lift);
    border-radius: var(--r-sm);
    color: var(--ember-lift);
    font-size: 12.5px;
    line-height: 1.45;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }
  th {
    text-align: left;
    font-weight: 500;
    color: var(--ink-4);
    font-size: 11px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    padding-bottom: 6px;
  }
  td {
    border-top: 1px solid var(--line);
    padding: 9px 8px 9px 0;
    vertical-align: top;
  }
  .creds {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
  }
  .creds input {
    min-width: 130px;
    flex: 1;
  }
  td.actions {
    text-align: right;
  }
  .err {
    color: var(--ember-lift);
    font-size: 12.5px;
  }

  @media (max-width: 720px) {
    .grid {
      grid-template-columns: 1fr;
    }
    table,
    thead,
    tbody,
    tr,
    td,
    th {
      display: block;
    }
    thead {
      display: none;
    }
    td {
      padding: 6px 0;
    }
    tr {
      border-top: 1px solid var(--line);
      padding: 8px 0;
    }
    td.actions {
      text-align: left;
    }
  }
</style>
