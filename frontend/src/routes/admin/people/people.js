// How a person reads on People: a colour, a role word, and every fact §6.6's roster row carries.
export { avatarColour } from '$lib/components/Avatar.svelte';

/** @param {string} role */
export const roleWord = (role) => (role === 'admin' ? 'Admin' : 'Member');

/** "Admin · you", "Member · disabled · linked to Jellyfin". */
export function subtitle(u, selfId) {
  const facts = [roleWord(u.role)];
  if (u.id === selfId) facts.push('you');
  if (!u.is_active) facts.push('disabled');
  if (u.jellyfin_user_id) {
    facts.push(u.jellyfin_link_state === 'needs_relink' ? 'Jellyfin needs sign-in' : 'linked to Jellyfin');
  }
  return facts.join(' · ');
}

/** "2 passkeys · PIN set", "No passkey · no PIN". */
export function signIn(u) {
  const keys = !u.passkeys ? 'No passkey' : u.passkeys === 1 ? '1 passkey' : `${u.passkeys} passkeys`;
  return `${keys} · ${u.has_pin ? 'PIN set' : 'no PIN'}`;
}
