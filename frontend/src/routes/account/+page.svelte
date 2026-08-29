<script>
  import Milestone from '$lib/components/Milestone.svelte';
  const points = [
    "Passkeys are primary and bound to the public origin; changing it invalidates every registered credential.",
    "A PIN enables fast switching on a shared or TV device.",
    "Logging out clears the session cookie only - passkeys remain registered."
];
</script>

<Milestone surface="Account" milestone="M1" summary="Passkeys, PIN, and the Jellyfin link for this account." {points} />
