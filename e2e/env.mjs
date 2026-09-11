/**
 * The stack's own `.env`, read the way `docker compose` reads it.
 *
 * Extracted from `reset.mjs` when the roadmap's parallel milestone pairs put a checkout per lane
 * on one machine. `reset.mjs` had always read PUBLIC_URL from here; `run.mjs` and
 * `playwright.config.js` each carried `http://localhost:8080` as a CONSTANT — so a second
 * worktree's suite reset its own database and then drove the FIRST worktree's app. Phase one
 * skipped (that stack is long past first boot, which `01-first-boot.spec.js` correctly reports as
 * a skip rather than a failure), and thirteen phase-two specs failed against an application on a
 * different branch. Same class as the two published ports before it: a constant where a
 * per-checkout value belongs.
 */
import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

// Read the way `docker compose` reads the same file, because this script and the stack it resets
// have to agree about what the stack's PUBLIC_URL *is*. Compose accepts `export KEY=v`, quoted
// values and a trailing ` #` comment; splitting on `=` and trimming refused all three, and a
// value compose accepts but this parser mangles is how a quoted PUBLIC_URL became "not a
// development stack" — one line above the guard that stands between an operator and their data.
// Four lines rather than a `dotenv` dependency: the e2e harness has none and this is not the
// place to acquire one. [M4.8, finding 9]
export function env(key) {
  const file = join(ROOT, '.env');
  if (!existsSync(file)) return undefined;
  // The whole file, and the LAST assignment of the key rather than the first: compose's dotenv,
  // `python-dotenv` (which is how `core/config.py` reads this same file into the app's settings)
  // and `sh` all build a map in file order, so a second `PUBLIC_URL=` appended under the first is
  // the one the stack boots on. Returning at the first match read a line the stack is not
  // running on, and the guard below then cleared a *production* URL as a development stack --
  // one statement above `DROP DATABASE`, which is the one place in this repository where being
  // approximately right about a value is not good enough. The fidelity claimed is over the
  // file's own lines: compose lets the shell environment beat `.env` entirely, and nothing this
  // script can read says whether it did. [M4.8 review cycle 2, E2E-1]
  let hit;
  for (const line of readFileSync(file, 'utf8').split(/\r?\n/)) {
    const [k, ...rest] = line.replace(/^\s*export\s+/, '').split('=');
    if (k.trim() !== key) continue;
    const raw = rest.join('=').trim();
    // Compose's rule in compose's ORDER — cut the trailing ` #` comment, then take what one
    // matching pair of quotes encloses. A quoted value keeps a `#` of its own; an unquoted one
    // ends at the first ` #`. Testing the whole right-hand side for quoting first is what
    // shipped, and `PUBLIC_URL="http://localhost:8080" # dev` — a line compose accepts and the
    // stack boots on — matches neither arm of it: the string ends in `v`, so the quote test
    // fails, and the fallback strips the comment and hands the guard below a value with its
    // quotes still attached. Each of the three forms worked alone; the composite of two of them
    // was the defect this function exists to end, one shape later. [M4.8, finding 9]
    const quoted = /^(['"])([\s\S]*?)\1\s*(?:#.*)?$/.exec(raw);
    hit = quoted ? quoted[2] : raw.replace(/\s+#.*$/, '').trim();
  }
  return hit;
}


/** The origin the stack actually serves, for a harness that must not guess which one it is. */
export function baseUrl() {
  return process.env.BASE_URL ?? env('PUBLIC_URL') ?? 'http://localhost:8080';
}
