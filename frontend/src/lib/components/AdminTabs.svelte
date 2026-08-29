<script>
  /**
   * §6.6's four admin cards as a tab row: Connectors / Data / Users / System.
   *
   * A tab whose milestone has not arrived is a `<span>` with the milestone in its tooltip, not
   * a link — the same rule the nav rail follows. Showing the whole shape from day one is
   * deliberate; making half of it clickable into an empty page is not.
   */
  let { active } = $props();

  const TABS = [
    { key: 'connectors', label: 'Connectors', href: '/admin/connectors', milestone: 'M1' },
    { key: 'data', label: 'Data', href: '/admin/data', milestone: 'M0' },
    { key: 'users', label: 'Users', href: null, milestone: 'M5' },
    { key: 'system', label: 'System', href: null, milestone: 'M5' }
  ];
</script>

<div class="tabs">
  {#each TABS as tab (tab.key)}
    {#if tab.key === active}
      <span class="pill on" aria-current="page">{tab.label}</span>
    {:else if tab.href}
      <a class="pill" href={tab.href}>{tab.label}</a>
    {:else}
      <span class="pill" title={tab.milestone}>{tab.label}</span>
    {/if}
  {/each}
</div>

<style>
  .tabs {
    display: flex;
    gap: 6px;
    margin-bottom: 18px;
  }
  a.pill {
    text-decoration: none;
  }
</style>
