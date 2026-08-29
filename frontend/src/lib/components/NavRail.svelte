<script>
  /**
   * The nav rail. Surface names are normative (spec §6): Home / Rate / Tonight / Rank / Map /
   * Taste. The prototype called Map "Explore" and hid Taste in the account menu; the spec's
   * names win, and every surface is visible with the milestone that owns it, so the shape of
   * the finished app is legible from day one rather than appearing later as a surprise.
   */
  import { page } from '$app/stores';

  const surfaces = [
    { href: '/', label: 'Home', milestone: 'M0', icon: 'M3 8.5 10 3l7 5.5V17H3z M8 17v-5h4v5' },
    { href: '/rate', label: 'Rate', milestone: 'M2', icon: 'M3 4h9v12H3z M14.5 6.5 17 8v8.5' },
    { href: '/tonight', label: 'Tonight', milestone: 'M4', icon: 'M8.4 7.2 13 10l-4.6 2.8z' },
    { href: '/rank', label: 'Rank', milestone: 'M3', icon: 'M3 5h13 M3 10h9 M3 15h5' },
    { href: '/map', label: 'Map', milestone: 'M6', icon: 'M12.8 7.2 8.9 8.9 7.2 12.8l3.9-1.7z' },
    { href: '/taste', label: 'Taste', milestone: 'M6', icon: 'M4 15V8 M8 15V5 M12 15v-6 M16 15v-9' }
  ];

  const current = $derived($page.url.pathname);
  const isActive = (href) => (href === '/' ? current === '/' : current.startsWith(href));
</script>

<nav aria-label="Surfaces">
  {#each surfaces as s (s.href)}
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
        {#if s.label === 'Tonight' || s.label === 'Map'}<circle cx="10" cy="10" r="7" />{/if}
        <path d={s.icon} />
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
