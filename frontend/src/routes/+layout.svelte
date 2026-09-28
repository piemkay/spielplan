<script>
  // The shell owns what every surface shares: the tab bar, You, the model rail, the toast, and what
  // a failed boot or a lost session shows. The document scrolls (decision 527).
  import '$lib/design.css';
  import { onMount } from 'svelte';
  import { page, updated } from '$app/stores';
  import { beforeNavigate, goto } from '$app/navigation';
  import { bootstrap, session, clearUser, landingRoute, refreshUser } from '$lib/session.svelte.js';
  import { onUnauthenticated, post } from '$lib/api.js';
  import { reset as resetRank } from '$lib/rank.svelte.js';
  import { closeRail, followShowModel, modelRail, toggleRail } from '$lib/rail.svelte.js';
  import AccountChip from '$lib/components/AccountChip.svelte';
  import ModelRail from '$lib/components/ModelRail.svelte';
  import NavRail from '$lib/components/NavRail.svelte';
  import Toast from '$lib/components/Toast.svelte';
  import { topbar } from '$lib/topbar.svelte.js';

  let { children } = $props();

  const canAdmin = $derived(
    (session.user?.nav?.account ?? []).some((entry) => entry.key === 'admin')
  );

  // Admin routes re-prompt after 24h (§3.2); shown only on the surfaces that gate blocks.
  const needsReauth = $derived(
    !!session.user?.admin_reauth_required &&
      ($page.url.pathname.startsWith('/admin') || $page.url.pathname.startsWith('/setup'))
  );

  // A PIN session cannot reauth by password (§3.2), so the banner offers no form there.
  const pinSession = $derived(session.user?.auth_method === 'pin');
  let reauthPassword = $state('');
  let reauthError = $state('');
  let reauthBusy = $state(false);

  const bare = $derived(
    $page.url.pathname.startsWith('/setup') ||
      $page.url.pathname.startsWith('/login') ||
      $page.url.pathname.startsWith('/account/password')
  );

  const showModel = $derived(!!session.user?.show_model);

  // Admin brings its own navigation (§6.6); the member tab bar stays with the member surfaces.
  const adminRoute = $derived($page.url.pathname.startsWith('/admin'));

  const TITLES = [
    ['/rate', 'Rate'],
    ['/tonight', 'Tonight'],
    ['/rank', 'Rank'],
    ['/account', 'You'],
    ['/admin', 'Admin'],
    ['/map', 'Map'],
    ['/taste', 'Taste'],
    ['/login', 'Sign in'],
    ['/setup', 'Setup']
  ];
  const docTitle = $derived.by(() => {
    const hit = TITLES.find(([prefix]) => $page.url.pathname.startsWith(prefix));
    return hit ? `${hit[1]} · Spielplan` : 'Spielplan';
  });

  // Turning the preference off closes an open drawer; the rule is in `rail.svelte.js`.
  $effect(() => {
    followShowModel(session.user?.show_model);
  });

  // The `m` shortcut (proposal 118), except while typing, and never on `bare` routes.
  function onRailKey(event) {
    if (event.key !== 'm' || event.metaKey || event.ctrlKey || event.altKey) return;
    const el = event.target;
    if (el instanceof HTMLElement) {
      const tag = el.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable) return;
    }
    toggleRail(showModel && !bare);
  }

  // Routes reachable before there is a signed-in user.
  const PUBLIC = ['/login', '/setup', '/account/password'];

  // Nobody is signed in and `/setup/state` did not answer, so /setup and /login are both possible:
  // say so and ask again rather than render the authed shell for nobody (decision 286).
  const landingUnknown = $derived(
    session.booted && !session.offline && !session.setup && !session.user
  );

  let retrying = $state(false);

  onMount(() => {
    topbar.host = true;
    // A 401 that `api.js` reads as a lost session lands here once, instead of on each surface.
    onUnauthenticated((reason) => {
      if (reason === 'password-change') {
        // An admin reset the password under an open tab: send it to the one screen that clears it.
        goto('/account/password');
        return;
      }
      clearUser();
      resetRank();
      goto('/login');
    });

    // The fast path: `online` reports the interface, not the appliance; the timer covers the rest.
    window.addEventListener('online', reconnect);

    // Through `reconnect()`, so an `online` flap during the first boot cannot start a second one.
    reconnect();

    return () => {
      topbar.host = false;
      onUnauthenticated(null);
      window.removeEventListener('online', reconnect);
    };
  });

  // Single-flight: two boots in flight would race their writes into one `session`.
  async function reconnect() {
    if (retrying) return;
    retrying = true;
    try {
      await bootstrap();
    } catch {
      session.offline = true;
    } finally {
      retrying = false;
    }
    await guard($page.url.pathname);
  }

  // `online` fires for the interface, not the appliance: a restarting box or a Tailscale route
  // comes back with no event. Armed only while offline or `landingUnknown`, so it costs nothing else.
  const RETRY_MS = 5_000;
  $effect(() => {
    if (!session.offline && !landingUnknown) return;
    const timer = setInterval(reconnect, RETRY_MS);
    return () => clearInterval(timer);
  });

  // Every navigation is guarded, so a deep link with no session is routed, not half-rendered.
  $effect(() => {
    if (!session.booted) return;
    guard($page.url.pathname);
  });

  async function guard(pathname) {
    // Unreachable: where a person belongs is unknown, and routing on a guess signed people out.
    if (session.offline) return;
    // Neither read arrived, so the destination is unknown; `landingUnknown` shows that instead.
    if (!session.setup && !session.user) return;
    const target = landingRoute();
    // /setup stays reachable for a signed-in admin (the wizard continues past its first step);
    // anyone else is bounced once `required` is false.
    if (pathname === '/setup' && !session.setup?.required && session.user?.role !== 'admin') {
      await goto(target);
      return;
    }
    if (target === '/') {
      if (pathname === '/login') await goto('/');
      return;
    }
    if (!PUBLIC.includes(pathname)) await goto(target);
  }

  // Set while a logout's fallback `goto` must not become a document load (see `beforeNavigate`).
  let leaving = false;

  async function logout() {
    // Only the POST is guarded: the local clears below must happen either way.
    let ended = true;
    try {
      await post('/auth/logout');
    } catch {
      // The cookie is HttpOnly, so all this page can do is stop acting as though it holds a session.
      ended = false;
    }
    clearUser();
    // Rank's card, pair and round must not carry into the next person's session.
    resetRank();
    // Leave the document so every module store is cleared, by a navigation that names /login (a
    // reload could land elsewhere). Only once the server ended the session, or the reload would
    // bring the same person straight back to Home.
    if (ended) location.assign('/login');
    else {
      // Keep the deploy hook off this navigation: as a document load it would sign the person back in.
      leaving = true;
      try {
        await goto('/login');
      } finally {
        leaving = false;
      }
    }
  }

  async function reauth(event) {
    event.preventDefault();
    reauthError = '';
    reauthBusy = true;
    try {
      await post('/auth/reauth', { password: reauthPassword });
      reauthPassword = '';
      // The reauth answer lacks fields the shell renders, so re-read `/auth/me`.
      await refreshUser();
    } catch (err) {
      reauthError = err.message;
    } finally {
      reauthBusy = false;
    }
  }

  // Take a pending deploy at a route change the person chose, not on a failed chunk import mid-tap.
  beforeNavigate((nav) => {
    if ($updated && !nav.willUnload && !leaving && nav.to?.url) location.href = nav.to.url.href;
  });
</script>

<svelte:window onkeydown={onRailKey} />
<svelte:head><title>{docTitle}</title></svelte:head>

<!-- A snippet: /setup is bare yet an admin surface, so the banner renders outside the shell too. -->
{#snippet reauthBanner()}
    <!-- Reauth in place: a link to /login is bounced straight back for a live session. -->
    <div class="reauth" role="alert" data-testid="admin-reauth">
      {#if pinSession}
        <span>
          This profile was switched into with a PIN, which is a convenience on a device
          someone is already signed in on — not proof of who is holding it. Sign in with
          your password or a passkey to reach the admin view.
        </span>
      {:else}
        <span>
          This admin session is more than 24 hours old. Confirm your password to carry on.
        </span>
        <form class="reauthform" onsubmit={reauth}>
          <input
            type="password"
            autocomplete="current-password"
            placeholder="password"
            bind:value={reauthPassword}
            aria-label="Password"
          />
          <button class="btn-primary" type="submit" disabled={reauthBusy || !reauthPassword}>
            {reauthBusy ? 'Checking…' : 'Confirm'}
          </button>
        </form>
      {/if}
      {#if reauthError}<span class="reautherr">{reauthError}</span>{/if}
    </div>
{/snippet}

{#if session.loading && !session.booted}
  <div class="boot"><span class="footnote">Connecting…</span></div>
{:else if bare}
  {#if needsReauth}{@render reauthBanner()}{/if}
  {@render children()}
{:else if session.offline}
  <!-- Replaces the shell: every surface behind it would paint from a payload nobody could fetch. -->
  <div class="boot">
    <div class="card unreachable" data-testid="appliance-unreachable">
      <h1>Spielplan is not answering</h1>
      <!-- Conditional: on a cold boot `session.user` is null, so "still signed in" would be false. -->
      <p class="why">
        The app is cached on this device, so it opened; the appliance did not answer the request
        that says who is holding the phone.
        {#if session.user}
          You are still signed in and nothing was sent.
        {:else}
          Nothing was sent, and nothing on this device has signed anybody out.
        {/if}
      </p>
      <button class="btn-primary" onclick={reconnect} disabled={retrying}>
        {retrying ? 'Trying' : 'Try again'}
      </button>
    </div>
  </div>
{:else if landingUnknown}
  <div class="boot">
    <div class="card unreachable" data-testid="landing-unknown">
      <h1>Spielplan did not finish answering</h1>
      <p class="why">
        This device was told that nobody is signed in here. It also asked whether this household
        has been set up yet, and that answer did not arrive — so it cannot tell the sign-in page
        from the first-boot wizard. Nothing was sent, and it is asking again.
      </p>
      <button class="btn-primary" onclick={reconnect} disabled={retrying}>
        {retrying ? 'Trying' : 'Try again'}
      </button>
    </div>
  </div>
{:else if !session.user}
  <!-- Children never mount for nobody, so a signed-out load fires no authenticated reads. -->
  <div class="boot"><span class="footnote">Connecting…</span></div>
{:else}
  <div class="shell" class:admin={adminRoute}>
    {#if !adminRoute}<NavRail />{/if}
    <div class="page">
      <header class="topbar" class:owned={!!topbar.content}>
        {#if topbar.content}
          {@render topbar.content()}
        {:else if session.restartRequired}
          <!-- Imported but not loaded (decision 497): the admin gets Movie data, a member plain words. -->
          {#if canAdmin}
            <a class="badge warn" href="/admin/movie-data">Restart needed to load the new movie data</a>
          {:else}
            <span class="badge warn">Waiting for a restart</span>
          {/if}
        {/if}
        {#if !topbar.content && session.hasBundle === false}
          <!-- `=== false`: null means `/config` did not answer, and this badge states a fact. -->
          {#if !session.restartRequired}
            {#if canAdmin}
              <a class="badge warn" href="/admin/movie-data">No movie data yet — import it</a>
            {:else}
              <span class="badge warn">No movie data yet</span>
            {/if}
          {/if}
        {/if}
        <AccountChip onLogout={logout} />
      </header>
      <main>
        {#if needsReauth}{@render reauthBanner()}{/if}
        {@render children()}
      </main>
    </div>
  </div>
  <!-- Outside `.shell`: fixed-position chrome, and `bare` routes have no chip to open it. -->
  <ModelRail open={modelRail.open} onClose={closeRail} suppressed={modelRail.suppressed} />
  <Toast />
{/if}

<style>
  /* `100dvh` after `100vh`: in an iOS Safari tab 100vh is the large viewport, which puts the
     bottom bar under the toolbar; 100vh stays as the fallback for iOS < 15.4. */
  .boot {
    height: 100vh;
    height: 100dvh;
    display: grid;
    place-items: center;
    background: var(--ground);
  }
  .shell {
    min-height: 100vh;
    min-height: 100dvh;
  }
  .unreachable {
    width: min(420px, 100%);
    margin: 24px;
    display: flex;
    flex-direction: column;
    gap: 12px;
    align-items: flex-start;
  }
  .unreachable h1 {
    margin: 0;
    font-family: var(--serif);
    font-weight: 400;
    font-size: var(--fs-title);
    line-height: 34px;
  }
  /* The installed app draws the status bar over the top edge (viewport-fit=cover): the top row
     carries the inset, and both rows respect a landscape notch. */
  .topbar {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 8px;
    min-height: calc(44px + env(safe-area-inset-top));
    padding: env(safe-area-inset-top) max(var(--gutter), env(safe-area-inset-right)) 0
      max(var(--gutter), env(safe-area-inset-left));
  }
  .topbar .badge {
    margin-right: auto;
  }
  .topbar.owned {
    gap: 0;
  }
  .topbar.owned > :global(:first-child) {
    flex: 1;
    min-width: 0;
  }
  main {
    --main-pad-end: calc(var(--tabbar) + env(safe-area-inset-bottom) + 32px);
    padding: 0 max(var(--gutter), env(safe-area-inset-right)) var(--main-pad-end)
      max(var(--gutter), env(safe-area-inset-left));
  }
  .reauth {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px 14px;
    flex-wrap: wrap;
    margin-bottom: 16px;
    padding: 12px 16px;
    background: var(--warning-tint);
    border-radius: var(--r-md);
    font-size: var(--fs-subhead);
  }
  .reauthform {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  .reautherr {
    flex-basis: 100%;
    color: var(--negative);
    font-size: var(--fs-subhead);
  }

  .admin main {
    --main-pad-end: calc(env(safe-area-inset-bottom) + 32px);
  }

  @media (min-width: 721px) {
    .shell {
      display: flex;
    }
    .page {
      flex: 1;
      min-width: 0;
    }
    .topbar {
      padding: 16px 40px 0;
    }
    main {
      --main-pad-end: 56px;
      padding: 0 40px var(--main-pad-end);
    }
  }
</style>
