<script>
  import Milestone from '$lib/components/Milestone.svelte';
  const points = [
    "Dropping a title between two others writes a tier edit plus two margin-less duels against its new neighbours.",
    "A straddling title shows both tiers and becomes queue-eligible.",
    "On phones: tap a poster to pick it up, tap a tier to drop it.",
    "One comparison in ten is uniform-random and held out - it is the only honest way to evaluate the tier model."
];
</script>

<Milestone surface="Rank" milestone="M3" summary="Your tier list - F through S - with learned cutpoints, drag-and-drop that the model treats as data rather than override, and a comparison queue that sharpens the boundaries." {points} />
