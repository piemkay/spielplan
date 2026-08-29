<script>
  /**
   * The nav rail. Surface names are normative (spec §6): Home / Rate / Tonight / Rank / Map /
   * Taste. The prototype called Map "Explore" and hid Taste in the account menu; the spec's
   * names win, and every surface is visible with the milestone that owns it, so the shape of
   * the finished app is legible from day one rather than appearing later as a surprise.
   *
   * The list comes from `/auth/me` rather than from here. §6.6 is admin-role only, and a
   * client-side `{#if role === 'admin'}` hides a link from someone reading the screen while
   * showing it to anyone reading the response. Navigation is a server decision.
   */
  import { page } from '$app/stores';
  import { session } from '$lib/session.svelte.js';

  // Presentation only, keyed by the server's surface key.
  const ICONS = {
    home: 'M3 8.5 10 3l7 5.5V17H3z M8 17v-5h4v5',
    rate: 'M3 4h9v12H3z M14.5 6.5 17 8v8.5',
    tonight: 'M8.4 7.2 13 10l-4.6 2.8z',
    rank: 'M3 5h13 M3 10h9 M3 15h5',
    map: 'M12.8 7.2 8.9 8.9 7.2 12.8l3.9-1.7z',
    taste: 'M4 15V8 M8 15V5 M12 15v-6 M16 15v-9'
  };

  const surfaces = $derived(session.user?.nav?.surfaces ?? []);
  const current = $derived($page.url.pathname);
  const isActive = (href) => (href === '/' ? current === '/' : current.startsWith(href));
</script>

<nav aria-label="Surfaces">
  {#each surfaces as s (s.key)}
    <a href={s.href} class:on={isActive(s.href)} title={s.label} data-surface={s.label}>
      <svg
        width="17"
        height="17"
        viewBox="0 0 20 20"
        fill="none"
        stroke="currentColor"
        stroke-width="1.4"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
      >
        {#if s.key === 'tonight' || s.key === 'map'}<circle cx="10" cy="10" r="7" />{/if}
        <path d={ICONS[s.key]} />
      </svg>
      <span class="label">{s.label}</span>
    </a>
  {/each}
</nav>

<style>
  nav {
    flex: none;
    width: 152px;
    border-right: 1px solid var(--line);
    background: var(--ground-raised);
    padding: 10px 8px;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  a {
    display: flex;
    align-items: center;
    gap: 11px;
    padding: 10px 12px;
    border-radius: var(--r-sm);
    color: var(--ink-3);
    font-size: 13px;
    min-height: 40px;
  }
  a:hover {
    filter: brightness(1.25);
  }
  a.on {
    background: var(--ember-wash);
    color: var(--ember-lift);
  }
  svg {
    flex: none;
  }

  @media (max-width: 720px) {
    nav {
      width: 100%;
      flex-direction: row;
      justify-content: space-around;
      border-right: none;
      border-top: 1px solid var(--line);
      padding: 6px 4px;
      padding-bottom: max(6px, env(safe-area-inset-bottom));
    }
    a {
      flex-direction: column;
      gap: 3px;
      min-width: var(--touch);
      min-height: var(--touch);
      justify-content: center;
      padding: 6px 4px;
    }
    .label {
      font-size: 10px;
    }
  }
</style>
