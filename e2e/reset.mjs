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

function env(key) {
  const file = join(ROOT, '.env');
  if (!existsSync(file)) return undefined;
  for (const line of readFileSync(file, 'utf8').split(/\r?\n/)) {
    const [k, ...rest] = line.split('=');
    if (k.trim() === key) return rest.join('=').trim();
  }
  return undefined;
}

const publicUrl = env('PUBLIC_URL') ?? '';
if (!/^https?:\/\/(localhost|127\.0\.0\.1)/.test(publicUrl)) {
  console.error(
    `refusing to reset: PUBLIC_URL is ${publicUrl || '(unset)'}, which does not look like a\n` +
      'development stack. This script destroys the database.'
  );
  process.exit(1);
}

const user = env('POSTGRES_USER') ?? 'spielplan';
const db = env('POSTGRES_DB') ?? 'spielplan';

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

// Wait for the backend to apply migrations and answer.
const base = publicUrl.replace(/\/$/, '');
for (let i = 0; i < 60; i++) {
  try {
    const res = await fetch(`${base}/api/health`);
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
console.error('the backend did not become healthy within 60s');
process.exit(1);
