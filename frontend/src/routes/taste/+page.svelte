<script>
  /**
   * §12's Taste placeholder, reading its milestone from `/auth/me`'s nav payload for the reason
   * `map/+page.svelte` argues at length: `api/auth.py`'s `SURFACES` is where §12's build order
   * is stated to the client, and a second copy of it here is a claim with nothing keeping it
   * true. [ds08-nav-rail-milestone-claim-is-false-and-the-value-is-duplicated]
   */
  import { session } from '$lib/session.svelte.js';
  import Milestone from '$lib/components/Milestone.svelte';

  const milestone = $derived(
    (session.user?.nav?.surfaces ?? []).find((s) => s.key === 'taste')?.milestone ?? ''
  );
  const points = [
    "Divisive titles are explained in vocabulary terms - who is pulled by what.",
    "Divergence predicts a night below your usual. It never predicts that anyone will hate it.",
    "The shared sweet spot doubles as the couple's watch-now prior and as the lens for what to acquire next."
];
</script>

<Milestone surface="Taste" {milestone} summary="Any two profiles side by side: facet silhouette, the seven shipped taste axes, the titles you divide on, and the region you both like." {points} />
