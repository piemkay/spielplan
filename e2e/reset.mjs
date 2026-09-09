/**
 * Reset the app to first boot, so `01-first-boot.spec.js` can test the sequence it is named
 * after rather than skipping.
 *
 * Drops and recreates the application database, clears the staged artifacts, and restarts the
 * app services. Requires the docker compose stack. Destroys all local data — it refuses to run
 * against anything that does not look like a development stack.
 *
 *   node e2e/reset.mjs
 */
import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const COMPOSE = [
  'compose',
  '-f', 'docker-compose.yml',
  '-f', 'ops/compose.dev.yml',
  // §7.3's two-way sync needs a Jellyfin that answers; ops/compose.e2e.yml provides a fake.
  '-f', 'ops/compose.e2e.yml',
];

function docker(args, opts = {}) {
  return execFileSync('docker', args, { cwd: ROOT, encoding: 'utf8', stdio: 'pipe', ...opts });
}

// Read the way `docker compose` reads the same file, because this script and the stack it resets
// have to agree about what the stack's PUBLIC_URL *is*. Compose accepts `export KEY=v`, quoted
// values and a trailing ` #` comment; splitting on `=` and trimming refused all three, and a
// value compose accepts but this parser mangles is how a quoted PUBLIC_URL became "not a
// development stack" — one line above the guard that stands between an operator and their data.
// Four lines rather than a `dotenv` dependency: the e2e harness has none and this is not the
// place to acquire one. [M4.8, finding 9]
function env(key) {
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

const publicUrl = env('PUBLIC_URL') ?? '';
if (!/^https?:\/\/(localhost|127\.0\.0\.1)/.test(publicUrl)) {
  console.error(
    `refusing to reset: PUBLIC_URL is ${publicUrl || '(unset)'}, which does not look like a\n` +
      'development stack. This script destroys the database.'
  );
  process.exit(1);
}

// Built here, before anything is destroyed, and reused as the health URL at the bottom of this
// file. The health loop was the first thing that ever noticed an unusable PUBLIC_URL, and by
// then the database was gone: the operator read "the backend did not become healthy" with no way
// to connect it to a stray comment on one line of their .env. The prefix test above
// passes a value with a space in it, `new URL` does not. [M4.8, finding 9]
let health;
try {
  health = new URL('/api/health', publicUrl);
} catch {
  console.error(
    `refusing to reset: PUBLIC_URL is ${JSON.stringify(publicUrl)}, which is not a usable URL`
  );
  process.exit(1);
}

const user = env('POSTGRES_USER') ?? 'spielplan';
const db = env('POSTGRES_DB') ?? 'spielplan';
// These two are interpolated into `psql -c` as SQL identifiers. Compose would accept
// `POSTGRES_DB="spielplan dev"` happily; this script refuses it rather than sending
// `DROP DATABASE IF EXISTS spielplan dev WITH (FORCE);` to a server and discovering what that
// means. Refused, not quoted: a value with whitespace in it is a misconfigured stack either way,
// and this script is about to drop a database. [M4.8, finding 9]
for (const [name, value] of [['POSTGRES_USER', user], ['POSTGRES_DB', db]]) {
  if (value === '' || /\s/.test(value)) {
    console.error(
      `refusing to reset: ${name} is ${JSON.stringify(value)}, which is not a single identifier`
    );
    process.exit(1);
  }
}

console.log('stopping app services…');
docker([...COMPOSE, 'stop', 'backend', 'worker']);

console.log(`dropping and recreating ${db}…`);
docker([...COMPOSE, 'exec', '-T', 'db', 'psql', '-U', user, '-d', 'postgres',
  '-c', `DROP DATABASE IF EXISTS ${db} WITH (FORCE);`]);
docker([...COMPOSE, 'exec', '-T', 'db', 'psql', '-U', user, '-d', 'postgres',
  '-c', `CREATE DATABASE ${db};`]);

console.log('clearing staged artifacts…');
// The contents, never the directory. `./data/artifacts` is a bind mount and the app containers run
// as uid 1000 (§14.3, `USER spielplan` in ops/backend.Dockerfile), so the ownership of the mount
// source is what decides whether the backend can create `/data/artifacts/<version>` at all.
// Removing the directory hands that decision to Docker, which recreates a missing mount source as
// **root** on the next `up` a few lines below — and the `chown -R 1000:1000` that
// .github/workflows/ci.yml runs before the stack starts has by then already been spent. Phase
// one's bundle import is the first thing that fails, on every Linux run, with a permission error
// nothing connects to this line. Emptying keeps the directory the chown established. [M4.7 sec-08]
const artifacts = join(ROOT, 'data', 'artifacts');
// Created here rather than left to Docker for the same reason: a directory this script makes
// belongs to whoever runs it, which README's chown line can then correct; one Docker makes belongs
// to root, which it cannot.
mkdirSync(artifacts, { recursive: true });
for (const entry of readdirSync(artifacts)) {
  try {
    rmSync(join(artifacts, entry), { recursive: true, force: true });
  } catch (err) {
    // The other side of the same rule: a version staged by the container is owned by uid 1000, and
    // a host user who is not it cannot unlink it. Say which command clears it while keeping the
    // directory, rather than leaving an EACCES stack to be read as a broken app.
    throw new Error(
      `cannot clear ${artifacts} (${err.code}): it holds files the container wrote as uid 1000. ` +
        'Run `sudo rm -rf data/artifacts/*` (the contents, not the directory) and try again.'
    );
  }
}

// Keeping the directory is necessary and not sufficient: it also has to be writable by uid 1000,
// and on a developer's machine nothing has ever made it so. CI is the case this was written for
// (`.github/workflows/ci.yml` chowns the five mounts before the stack starts, and emptying rather
// than removing is what stops that chown being spent) — but a checkout that has never run CI has
// a `data/artifacts` owned by whoever cloned it, and `docker compose up` then hands the backend a
// directory it cannot create a version in. Measured here, on Docker Desktop: phase 1's import
// answered 500 with `PermissionError: [Errno 13] Permission denied: '/data/artifacts/test-v1'`,
// and the spec reported only a missing "artifacts staged to" finding.
//
// Done from a root container rather than from the host because that is the one gesture that works
// everywhere. `chown` on the host needs privileges the script does not have and, under Docker
// Desktop's Windows file sharing, does not reach the container's view at all — the mode seen
// inside is the one the VM holds, and only a container can set it. Both are attempted: `chown` is
// the honest fix where uids mean something, `chmod` the fallback where they do not. Failure is
// not fatal, because a mount that is already writable needs neither. [M4.7 sec-08]
try {
  docker([...COMPOSE, 'run', '--rm', '--user', '0', '--entrypoint', 'sh', 'backend',
    '-c', 'chown -R 1000:1000 /data/artifacts 2>/dev/null || chmod -R 777 /data/artifacts']);
} catch (err) {
  console.log(`  could not adjust ${artifacts} ownership (${err.code ?? 'failed'}) — continuing; ` +
    'if the bundle import fails with a permission error, this line is why');
}

console.log('starting app services…');
// Retried, because `depends_on: db: service_healthy` is evaluated once and a Postgres that is
// briefly busy answers `pg_isready` with "no response". Dropping and recreating a database is
// exactly the moment it is busiest, so the first attempt can lose a race it would win a second
// later — and failing the whole suite for that reads as a broken app.
for (let attempt = 1; ; attempt++) {
  try {
    docker([...COMPOSE, 'up', '-d', 'backend', 'worker']);
    break;
  } catch (err) {
    if (attempt === 5) throw err;
    console.log(`  the database was not ready yet (attempt ${attempt}) — retrying`);
    await new Promise((r) => setTimeout(r, 3000));
  }
}

// Wait for the backend to apply migrations and answer. `health` was built and validated above,
// before the drop, rather than assembled here from a string this script had never parsed.
for (let i = 0; i < 60; i++) {
  try {
    // Each attempt carries its own deadline, for the reason `run.mjs`'s loop now does: `fetch`
    // has none, so a backend that accepts the connection and never answers holds iteration 1 for
    // ever and the loop is not a bound on anything. Same loop, same defect. [M4.8, finding 4]
    const res = await fetch(health, { signal: AbortSignal.timeout(5000) });
    if (res.ok) {
      const body = await res.json();
      console.log(`ready — bundle: ${body.bundle ?? 'none'}`);
      process.exit(0);
    }
  } catch {
    /* not up yet */
  }
  await new Promise((r) => setTimeout(r, 1000));
}
// Attempts, and the ceiling they actually buy: an attempt is a 5 s deadline plus a 1 s sleep, so
// sixty of them is six minutes and not the minute this sentence used to promise. The difference
// is exactly the case the deadline was added for -- against a backend that accepts and never
// answers, or a frozen Postgres the health route answers 503 for after up to 3x its own timeout
// (`app.py:90-102`), every attempt runs its deadline out. An operator told "60s" after six
// minutes reads a slow boot and raises the count; what the run measured was sixty stalls.
// [M4.8 review cycle 2, E2E-2]
console.error('the backend did not become healthy in 60 attempts (up to 6 minutes)');
process.exit(1);
