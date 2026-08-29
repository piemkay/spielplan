<script>
  import Milestone from '$lib/components/Milestone.svelte';
  const points = [
    "Axis definitions are an authored, shipped artifact - deterministic, so the map does not shift when a bundle is re-imported.",
    "Three lenses: facet colour, seen-state, match-for-you.",
    "Compositional search binds a query to a predicate over the vocabulary and shows it: has(robots) AND NOT has(gladiatorial).",
    "An empty predicate goes to the extraction queue - this is how the vocabulary grows where it is actually needed."
];
</script>

<Milestone surface="Map" milestone="M6" summary="Titles plotted on two user-selectable bipolar facet axes with named poles, with wander mode where every edge is labelled with the term it rides on." {points} />
