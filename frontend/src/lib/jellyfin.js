// The users endpoint answers 409 when unconfigured and 502 when Jellyfin is down: no options.

import { get } from './api.js';

export async function jellyfinDirectory() {
  const cfg = await get('/admin/connectors/jellyfin');
  const users = cfg.configured
    ? ((await get('/admin/connectors/jellyfin/users').catch(() => [])) ?? [])
    : [];
  return { cfg, users };
}
