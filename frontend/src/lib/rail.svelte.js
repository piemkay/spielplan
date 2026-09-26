// One drawer's state, shared by the shell that mounts it and by Home, which publishes the
// suppressed rows only its payload carries.

export const modelRail = $state({
  open: false,
  /** @type {any[]} Proposal 28's suppressed rows, published by `/` and read by the shell. */
  suppressed: []
});

// Guarded here, not at the call sites: the `m` shortcut must not open the rail with the numbers off.
export function toggleRail(showModel) {
  modelRail.open = showModel ? !modelRail.open : false;
}

export function closeRail() {
  modelRail.open = false;
}

// Follows the preference, not the epoch: the drawer refetches a server-gated route on every open.
export function followShowModel(showModel) {
  if (!showModel) modelRail.open = false;
}

// Emptied when Home is not on screen; `undefined` is ordinary, since the toggle off strips it.
export function publishSuppressed(list) {
  modelRail.suppressed = Array.isArray(list) ? list : [];
}
