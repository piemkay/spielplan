<script>
  /**
   * §12's Map placeholder. The milestone is READ from `/auth/me`'s nav payload rather than
   * written here.
   *
   * `api/auth.py`'s `SURFACES` is the one place §12's build order is stated to the client, and
   * it was being stated twice: once in a payload field nothing in `frontend/src` rendered, and
   * once as the literal "M6" on this page and on Taste. So a §12 change — and this project has
   * made several (decisions 165, 166, 181, 182) — would have had to be made in three files to
   * be true on the screen, which is the shape of a claim that goes stale. The rail renders a
   * label and an icon; the destination renders the milestone.
   * [ds08-nav-rail-milestone-claim-is-false-and-the-value-is-duplicated]
   */
  import { session } from '$lib/session.svelte.js';
  import Milestone from '$lib/components/Milestone.svelte';

  const milestone = $derived(
    (session.user?.nav?.surfaces ?? []).find((s) => s.key === 'map')?.milestone ?? ''
  );
  const points = [
    "Axis definitions are an authored, shipped artifact - deterministic, so the map does not shift when a bundle is re-imported.",
    "Three lenses: facet colour, seen-state, match-for-you.",
    "Compositional search binds a query to a predicate over the vocabulary and shows it: has(robots) AND NOT has(gladiatorial).",
    "An empty predicate goes to the extraction queue - this is how the vocabulary grows where it is actually needed."
];
</script>

<Milestone surface="Map" {milestone} summary="Titles plotted on two user-selectable bipolar facet axes with named poles, with wander mode where every edge is labelled with the term it rides on." {points} />
