<script>
  import Milestone from '$lib/components/Milestone.svelte';
  const points = [
    "Sweep: one card, Liked / Fine / Disliked, plus Not seen and Skip - one seen-state control, because a title you cannot remember is plain unseen.",
    "A running class balance: spreading verdicts across all three classes matters about five times more than anything else you can do here.",
    "Battle: two posters, a Tie that is first-class data, and a decisive toggle - a decisive pick teaches more than a hesitant one.",
    "Pairs drawn at random. For profiles no selection rule beats random; the clever ones only pay off in the tier queue."
];
</script>

<Milestone surface="Rate" milestone="M2" summary="Mix, sweep and battle over a P(seen)-ordered queue, in blocks of 15, feeding all four arms of the Personal Ledger." {points} />
