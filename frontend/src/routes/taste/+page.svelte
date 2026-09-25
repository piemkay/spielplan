<script>
  /**
   * §12's Taste placeholder, in the member register for the reason `map/+page.svelte` gives
   * (decisions 486, 488): it names no milestone, and its points are plain words.
   */
  import Milestone from '$lib/components/Milestone.svelte';

  const points = [
    'The films you two disagree on, and what in them pulls each of you.',
    'A split means a night a little below your usual - never that anyone will hate it.',
    'The films you would both enjoy, as the place to start on a night together.'
  ];
</script>

<Milestone
  surface="Taste"
  summary="Two people's tastes side by side: where they meet, and where they part."
  {points}
/>
