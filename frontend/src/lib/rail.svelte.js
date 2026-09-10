/**
 * The one model rail's shell state. Spec v2.1 §6.7, §6.8; decisions 117 and 118.
 *
 * §6.7: "A per-user toggle (default off) reveals an ephemeral log (last ~15 events, never
 * persisted) narrating every model write in one human-readable line … the primary M2 debugging
 * instrument." Proposal 118 asks for it "reachable in two taps" and from a keyboard shortcut.
 *
 * ONE DRAWER, NOT ONE PER ROUTE [M4.9 finding 25]. The toggle has always lived in the account
 * dropdown, which `AccountChip.svelte`'s own comment calls a thing "on every screen", while
 * `ModelRail` was imported and mounted exactly once, on Home. The three surfaces that write the
 * most model events therefore reached for substitutes — Rate's per-response echo, Rank's
 * single-element log, and an embedded `rail.recent(limit=5)` on Tonight, which is the surface
 * §6.7's own fourth worked example is written about. The mount is now in `routes/+layout.svelte`
 * beside the toggle that governs it, and this module is what the two ends share.
 *
 * IT IS A MODULE BECAUSE ONE THING THE DRAWER RENDERS IS HOME'S. Proposal 28's suppressed
 * (shelf, kind) rows are in the `/api/home` payload and nowhere else, and the shell has no Home
 * payload to read — so Home publishes them here and the shell renders them. The open flag came
 * with them rather than staying local to the layout: one drawer with its list in a shared module
 * and its open state in a component is one drawer owned by two files.
 *
 * NOT IN `home.svelte.js`. With the mount in the shell the drawer is shell chrome, and a shell
 * that imports a surface module to learn whether its own chrome is open has the dependency the
 * wrong way round. `home.svelte.js` keeps what is about the Home payload.
 */

export const modelRail = $state({
  /** Whether the one drawer is open. */
  open: false,
  /** @type {any[]} Proposal 28's suppressed rows, published by `/` and read by the shell. */
  suppressed: []
});

/**
 * Flip the drawer — the trigger button and proposal 118's `m` shortcut, one rule for both.
 *
 * The preference arrives as an argument rather than as an import: decision 117's switch lives on
 * `session.user`, and a state module that reached into the session to answer "may this open?"
 * would make two modules co-owners of one boolean. The guard is here rather than at the call
 * sites because one of those call sites is a keystroke — a rail that opens on `m` after the
 * person switched the numbers off is a back door into exactly what §6.7 promises is absent.
 */
export function toggleRail(showModel) {
  modelRail.open = showModel ? !modelRail.open : false;
}

export function closeRail() {
  modelRail.open = false;
}

/**
 * Decision 117's preference, applied to a drawer that is already open.
 *
 * `+page.svelte` did this before the mount moved, and it has to survive the move: the toggle is
 * three components away, and turning it off must take the drawer with it or the shell is left
 * rendering a panel whose control is gone.
 *
 * It follows the preference itself rather than `modelGate.epoch` — the "the write actually
 * landed on the server" signal Home refetches on — because the drawer is not a payload. It
 * refetches `/api/model-log` on every open and that route is gated server-side (`rail.visible_to`),
 * so there is nothing here to race: waiting for the epoch would only keep the drawer up for a
 * round trip after its own button had gone.
 */
export function followShowModel(showModel) {
  if (!showModel) modelRail.open = false;
}

/**
 * Home's suppressed list, on its way to a drawer Home no longer mounts.
 *
 * Emptied rather than kept when Home is not on screen: proposal 28's rows are a statement about
 * the shelves that were just built, and a list of what Home did not ship, still in the drawer on
 * Rate, is a claim about a surface that is not there. `undefined` is the ordinary input, not an
 * error — decision 117 strips `suppressed` from the payload with the toggle off.
 */
export function publishSuppressed(list) {
  modelRail.suppressed = Array.isArray(list) ? list : [];
}
