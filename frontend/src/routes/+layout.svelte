<script>
  /**
   * The app shell. Spec v2.1 §6 preamble (phone-first PWA), §6.8 (design language),
   * §3.2 (the account chip switches profiles, gated by the per-user PIN).
   *
   * Surface names are normative (§6): Home / Rate / Tonight / Rank / Map / Taste (+ Admin).
   * M0 ships Home; the rest are present as destinations that say what milestone owns them,
   * because a nav that hides half the app teaches the wrong shape.
   */
  import '$lib/design.css';
  import { onMount } from 'svelte';
  import { page } from '$app/stores';
  import { goto } from '$app/navigation';
  import { bootstrap, session, clearUser, landingRoute, refreshUser } from '$lib/session.svelte.js';
  import { post } from '$lib/api.js';
  import { reset as resetRank } from '$lib/rank.svelte.js';
  import AccountChip from '$lib/components/AccountChip.svelte';
  import NavRail from '$lib/components/NavRail.svelte';

  let { children } = $props();

  const canAdmin = $derived(
    (session.user?.nav?.account ?? []).some((entry) => entry.key === 'admin')
  );

  // §3.2: "admin routes re-prompt after 24 h". The gate is server-side and returns a 401 that
  // an admin can do nothing about from an admin page, so the shell says what it is and where
  // to go. Only on the surfaces it actually blocks — elsewhere it is not yet true of anything.
  const needsReauth = $derived(
    !!session.user?.admin_reauth_required &&
      ($page.url.pathname.startsWith('/admin') || $page.url.pathname.startsWith('/setup'))
  );

  // sec-05: a PIN session is refused by `POST /api/auth/reauth` however good the password
  // typed into it is (§3.2 makes it a switch convenience, not a sign-in), so the banner offers
  // no form there — and it must not claim the session is "more than 24 hours old", which is
  // not why this one is blocked.
  const pinSession = $derived(session.user?.auth_method === 'pin');
  let reauthPassword = $state('');
  let reauthError = $state('');
  let reauthBusy = $state(false);

  const bare = $derived(
    $page.url.pathname.startsWith('/setup') ||
      $page.url.pathname.startsWith('/login') ||
      $page.url.pathname.startsWith('/account/password')
  );

  // Routes reachable before there is a signed-in user.
  const PUBLIC = ['/login', '/setup', '/account/password'];

  onMount(async () => {
    await bootstrap();
    await guard($page.url.pathname);
  });

  // Every navigation is guarded, not only the landing one: a deep link to /rank with no
  // session used to render the authed shell and then fail request by request.
  $effect(() => {
    if (!session.booted) return;
    guard($page.url.pathname);
  });

  async function guard(pathname) {
    const target = landingRoute();
    // /setup is bounced for anyone who is not a signed-in admin, not for anyone once the
    // admin step is done (cs-33). §3.1's own sequence continues past that step — connectors,
    // then the bundle — and the old test bounced the operator to Home the instant the admin
    // row existed, which made steps 2 and 3 of a three-step wizard unreachable in the app
    // that ships. An admin revisiting the page is exactly who §3.1 means by "a revisitable
    // page after that" (`api/setup.py`'s own comment); a member is not, and neither is a
    // signed-out stranger — the rule ran only for a signed-IN caller, and sec-14 stopped
    // telling an anonymous one whether an admin exists, so the shell is the only thing left
    // that knows the wizard is over (fe-46). `required` is that bit, for everyone.
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

  async function logout() {
    // Only the POST is guarded. §3.2 makes logout "clears the session cookie only", and on a
    // LAN/Tailscale app a failed request is the ordinary failure: unguarded, it rejected out
    // of the click handler and left the menu open showing the name of a person whose session
    // the server may already have destroyed (feroutes-logout). The local clears below are the
    // half that must happen either way.
    try {
      await post('/auth/logout');
    } catch {
      // Nothing to report and nothing to retry: the cookie is HttpOnly, so the only thing this
      // page can still do about the session is stop acting as though it holds one.
    }
    clearUser();
    // Sign-out is a client-side navigation, so module-level surface state survives it. Rank's
    // lift is a *pending write naming a bare title id*, and carried into the next person's
    // session it would post a `tier_edit` into their append-only Ledger on the first tap.
    resetRank();
    await goto('/login');
  }

  async function reauth(event) {
    event.preventDefault();
    reauthError = '';
    reauthBusy = true;
    try {
      await post('/auth/reauth', { password: reauthPassword });
      reauthPassword = '';
      // The route answers with the same payload as /auth/me minus the passkey and Jellyfin
      // fields the shell also renders, so re-read rather than assigning the response.
      await refreshUser();
    } catch (err) {
      reauthError = err.message;
    } finally {
      reauthBusy = false;
    }
  }
</script>

<!-- sec-05, and decision 164's consequence: /setup is `bare` (no shell, no nav) AND it is
     now a revisitable admin surface, so the banner has to be renderable outside the shell
     as well as inside it. A snippet rather than two copies: an admin past 24 h who lands on
     the wizard would otherwise get no banner, no form and a 401 from every step. -->
{#snippet reauthBanner()}
    <!-- sec-05: the affordance here used to be a link to /login, which `guard()` bounces
         straight back for a live session — the banner's only way out led in a circle.
         `POST /auth/reauth` clears the stamp on the session in hand and mints nothing. -->
    <div class="reauth" role="alert" data-testid="admin-reauth">
      {#if pinSession}
        <span>
          This profile was switched into with a PIN, which §3.2 makes a convenience on a
          device someone is already signed in on — not proof of who is holding it. Sign
          in with your password or a passkey to reach the admin view.
        </span>
      {:else}
        <span>
          This admin session is more than 24 hours old (§3.2). Confirm your password to
          carry on.
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
  <div class="boot"><span class="data">connecting…</span></div>
{:else if bare}
  {#if needsReauth}{@render reauthBanner()}{/if}
  {@render children()}
{:else}
  <div class="shell">
    <header>
      <span class="brand">SPIELPLAN</span>
      <div class="spacer"></div>
      {#if !session.hasBundle}
        <!-- §3.1: a bundle-less app is a legal state, said out loud rather than crashed on.
             Only an admin gets a link out of it: importing is §6.6's Data tab, and offering a
             member a door they will meet a 403 behind is worse than stating the fact. -->
        {#if canAdmin}
          <a class="nobundle data" href="/admin/data">no bundle imported</a>
        {:else}
          <span class="nobundle data">no bundle imported</span>
        {/if}
      {/if}
      <AccountChip onLogout={logout} />
    </header>
    <div class="body">
      <NavRail />
      <main>
        {#if needsReauth}{@render reauthBanner()}{/if}
        {@render children()}
      </main>
    </div>
  </div>
{/if}

<style>
  .boot {
    height: 100vh;
    display: grid;
    place-items: center;
    background: var(--ground);
  }
  .shell {
    display: flex;
    flex-direction: column;
    height: 100vh;
  }
  header {
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 0 20px;
    height: 54px;
    flex: none;
    border-bottom: 1px solid var(--line);
    background: var(--ground-raised);
    position: relative;
    z-index: 60;
  }
  .brand {
    font-weight: 700;
    font-size: 14px;
    letter-spacing: 0.13em;
  }
  .spacer {
    flex: 1;
  }
  .reauth {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px 14px;
    flex-wrap: wrap;
    margin-bottom: 16px;
    padding: 12px 15px;
    border: 1px solid var(--ember-edge);
    background: var(--ember-wash);
    border-radius: var(--r-md);
    font-size: 13px;
  }
  .reauthform {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  .reautherr {
    flex-basis: 100%;
    color: var(--ember-lift);
    font-size: 12.5px;
  }
  .nobundle {
    border: 1px solid var(--ember-edge);
    background: var(--ember-wash);
    color: var(--ember-lift);
    padding: 5px 11px;
    border-radius: var(--r-pill);
  }
  .body {
    flex: 1;
    min-height: 0;
    display: flex;
  }
  main {
    flex: 1;
    min-width: 0;
    overflow: auto;
    padding: 20px 22px 40px;
  }

  /* Phone-first: the rail becomes a bottom bar and the header keeps only identity. */
  @media (max-width: 720px) {
    .body {
      flex-direction: column-reverse;
    }
    main {
      padding: 14px 14px 24px;
    }
  }
</style>
