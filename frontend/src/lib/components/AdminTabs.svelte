<script>
  /**
   * §6.6's four admin cards as a tab row: Connectors / Data / Users / System.
   *
   * A tab whose milestone has not arrived is a `<span>` carrying the milestone as visible
   * text, not a link — the same rule the nav rail follows. Showing the whole shape from day
   * one is deliberate; making half of it clickable into an empty page is not.
   *
   * Visible text and not `title=`: the milestone used to live in a tooltip, and the declared
   * primary form factor is a phone, which cannot hover (spec-13). The full sentence follows
   * the tabs for the same reason the surface placeholders carry it (§12, Milestone.svelte) —
   * "Not built yet" is the thing an unexplained grey pill fails to say.
   */
  let { active } = $props();

  const TABS = [
    { key: 'connectors', label: 'Connectors', href: '/admin/connectors', milestone: 'M1' },
    { key: 'data', label: 'Data', href: '/admin/data', milestone: 'M0' },
    // §12 gained the M4.6 row with this milestone; §6.6's Users card is what it builds.
    { key: 'users', label: 'Users', href: '/admin/users', milestone: 'M4.6' },
    { key: 'system', label: 'System', href: null, milestone: 'M5' }
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
