<script>
  /**
   * §12's Map placeholder, in the member register (decision 486).
   *
   * It read its milestone from `/auth/me`'s nav payload so that §12's order was stated in one
   * place [ds08-nav-rail-milestone-claim-is-false-and-the-value-is-duplicated]; since decision
   * 488 an unbuilt surface is not in that payload at all, and since decision 486 a member surface
   * names no milestone, so there is nothing left to read. The points describe the surface in
   * plain words: they carried the spec's predicate syntax and artifact nouns to members.
   */
  import Milestone from '$lib/components/Milestone.svelte';

  const points = [
    'Two axes you choose, each running between two named ends - from light to dark, say.',
    'Colour the map by what a film is like, by what you have seen, or by how well it suits you.',
    'Explore outward from any title: every step says what the two films share.'
  ];
</script>

<Milestone
  surface="Map"
  summary="Your library laid out on a map of what the films are like."
  {points}
/>
