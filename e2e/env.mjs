/** The stack's own `.env`, so each checkout's harness drives its own stack and no other. */
import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

// Compose's reading: `export KEY=v`, quotes, a trailing ` #` comment, and the LAST assignment
// wins. It must match exactly, because reset.mjs decides `DROP DATABASE` on PUBLIC_URL. A shell
// variable, which compose lets beat `.env`, is invisible here.
export function env(key) {
  const file = join(ROOT, '.env');
  if (!existsSync(file)) return undefined;
  let hit;
  for (const line of readFileSync(file, 'utf8').split(/\r?\n/)) {
    const [k, ...rest] = line.replace(/^\s*export\s+/, '').split('=');
    if (k.trim() !== key) continue;
    const raw = rest.join('=').trim();
    // A quoted value keeps a `#` of its own and may be followed by a comment; an unquoted one
    // ends at the first ` #`.
    const quoted = /^(['"])([\s\S]*?)\1\s*(?:#.*)?$/.exec(raw);
    hit = quoted ? quoted[2] : raw.replace(/\s+#.*$/, '').trim();
  }
  return hit;
}


export function baseUrl() {
  return process.env.BASE_URL ?? env('PUBLIC_URL') ?? 'http://localhost:8080';
}
