/**
 * The Jellyfin connector's configuration and its user list, read together.
 *
 * Two admin surfaces need the same pair (§3.3: "Admin view maps each app user <-> one Jellyfin
 * user (GET /Users)"): the Connectors card's user-mapping table, and §6.6's Users row editor,
 * which carries "Jellyfin re-link / unlink" and so needs the same picker. One reader, because
 * the guard is the part worth stating once — `/admin/connectors/jellyfin/users` answers 409
 * when nothing is configured and 502 when the server is down, and a picker that renders
 * neither case is a picker that silently has no options.
 */

import { get } from './api.js';

export async function jellyfinDirectory() {
  const cfg = await get('/admin/connectors/jellyfin');
  const users = cfg.configured
    ? ((await get('/admin/connectors/jellyfin/users').catch(() => [])) ?? [])
    : [];
  return { cfg, users };
}
