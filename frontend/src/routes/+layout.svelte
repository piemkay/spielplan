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
  import { bootstrap, session, clearUser, landingRoute } from '$lib/session.svelte.js';
  import { post } from '$lib/api.js';
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
    if (target === '/') {
      if (pathname === '/login' || (pathname === '/setup' && !session.setup?.required)) {
        await goto('/');
      }
      return;
    }
    if (!PUBLIC.includes(pathname)) await goto(target);
  }

  async function logout() {
    await post('/auth/logout');
    clearUser();
    await goto('/login');
  }
</script>

{#if session.loading && !session.booted}
  <div class="boot"><span class="data">connecting…</span></div>
{:else if bare}
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
        {#if needsReauth}
          <div class="reauth" role="alert">
            <span>
              This admin session is more than 24 hours old. Sign in again to reach the admin
              view.
            </span>
            <a class="btn-primary" href="/login">Sign in again</a>
          </div>
        {/if}
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
    gap: 14px;
    flex-wrap: wrap;
    margin-bottom: 16px;
    padding: 12px 15px;
    border: 1px solid var(--ember-edge);
    background: var(--ember-wash);
    border-radius: var(--r-md);
    font-size: 13px;
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
