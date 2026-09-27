<script>
  import Milestone from '$lib/components/Milestone.svelte';

  const points = [
    'Two axes you choose, each running between two named ends — from light to dark, say.',
    'Colour the map by what a film is like, by what you have seen, or by how well it suits you.',
    'Explore outward from any title: every step says what the two films share.'
  ];
</script>

<Milestone
  surface="Map"
  summary="Your library laid out on a map of what the films are like."
  {points}
/>
