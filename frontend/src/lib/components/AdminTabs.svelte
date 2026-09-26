<script>
  // An unbuilt tab is a span with its milestone as visible text: a phone cannot hover.
  let { active } = $props();

  const TABS = [
    { key: 'connectors', label: 'Connectors', href: '/admin/connectors', milestone: 'M1' },
    { key: 'data', label: 'Data', href: '/admin/data', milestone: 'M0' },
    { key: 'users', label: 'Users', href: '/admin/users', milestone: 'M4.6' },
    { key: 'system', label: 'System', href: '/admin/system', milestone: 'M4.7' }
  ];

  const pending = TABS.filter((tab) => !tab.href);
</script>

<div class="tabs">
  {#each TABS as tab (tab.key)}
    {#if tab.key === active}
      <span class="pill on" aria-current="page">{tab.label}</span>
    {:else if tab.href}
      <a class="pill" href={tab.href}>{tab.label}</a>
    {:else}
      <span class="pill pendtab">
        <span>{tab.label}</span>
        <span class="data tag">{tab.milestone}</span>
      </span>
    {/if}
  {/each}
</div>

{#each pending as tab (tab.key)}
  <p class="why pending">
    {tab.label}: Not built yet — this surface arrives with {tab.milestone}.
  </p>
{/each}

<style>
  .tabs {
    display: flex;
    gap: 6px;
    margin-bottom: 18px;
  }
  a.pill {
    text-decoration: none;
  }
  .pendtab {
    display: inline-flex;
    align-items: baseline;
    gap: 7px;
  }
  .tag {
    letter-spacing: 0.12em;
  }
  .pending {
    margin: -12px 0 16px;
  }
</style>
