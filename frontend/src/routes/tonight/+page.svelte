<script>
  import Milestone from '$lib/components/Milestone.svelte';
  const points = [
    "Join by push, room code, QR, the open-rooms list or the TV route - all equivalent, because push is best effort.",
    "Votes stay hidden until everyone has finished.",
    "A hard split is surfaced with the alternative in hand, never silently averaged.",
    "Solo mode: three picks and a wildcard from the same pool, each with a one-line why."
];
</script>

<Milestone surface="Tonight" milestone="M4" summary="A session that resolves a real Friday night: lobby, open rooms, roughly ten candidate votes per participant, a blind reveal, and one winner with its reasons." {points} />
