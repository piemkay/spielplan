/**
 * Reset the app to first boot: drops the database, clears the staged artifacts, restarts the app
 * services. Destroys local data, so it refuses anything that does not look like a dev stack.
 */
import { execFileSync } from 'node:child_process';
import { mkdirSync, readdirSync, rmSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { env } from './env.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const COMPOSE = [
  'compose',
  '-f', 'docker-compose.yml',
  '-f', 'ops/compose.dev.yml',
  '-f', 'ops/compose.e2e.yml',
];

function docker(args, opts = {}) {
  return execFileSync('docker', args, { cwd: ROOT, encoding: 'utf8', stdio: 'pipe', ...opts });
}


const publicUrl = env('PUBLIC_URL') ?? '';
if (!/^https?:\/\/(localhost|127\.0\.0\.1)/.test(publicUrl)) {
  console.error(
    `refusing to reset: PUBLIC_URL is ${publicUrl || '(unset)'}, which does not look like a\n` +
      'development stack. This script destroys the database.'
  );
  process.exit(1);
}

// Validated before anything is destroyed: the prefix test above passes a value with a space in it.
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
// Interpolated into `psql -c` as SQL identifiers, so whitespace is refused rather than quoted.
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
// The contents, never the directory: Docker recreates a missing bind-mount source as root, and the
// app runs as uid 1000, so the ownership CI's chown established would be lost.
const artifacts = join(ROOT, 'data', 'artifacts');
mkdirSync(artifacts, { recursive: true });
for (const entry of readdirSync(artifacts)) {
  try {
    rmSync(join(artifacts, entry), { recursive: true, force: true });
  } catch (err) {
    throw new Error(
      `cannot clear ${artifacts} (${err.code}): it holds files the container wrote as uid 1000. ` +
        'Run `sudo rm -rf data/artifacts/*` (the contents, not the directory) and try again.'
    );
  }
}

// It must also be writable by uid 1000, which a fresh checkout's is not. From a root container,
// because a host chown does not reach Docker Desktop's view; chmod where uids mean nothing.
try {
  docker([...COMPOSE, 'run', '--rm', '--user', '0', '--entrypoint', 'sh', 'backend',
    '-c', 'chown -R 1000:1000 /data/artifacts 2>/dev/null || chmod -R 777 /data/artifacts']);
} catch (err) {
  console.log(`  could not adjust ${artifacts} ownership (${err.code ?? 'failed'}) — continuing; ` +
    'if the bundle import fails with a permission error, this line is why');
}

console.log('starting app services…');
// Retried: `service_healthy` is evaluated once, and Postgres is busiest right after the drop.
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

// The fake's source is bind-mounted and read once at process start, and `up -d` leaves a running
// container alone: only a restart makes it serve the current file.
console.log('restarting the fake Jellyfin so it re-reads ops/fake_jellyfin.py…');
try {
  docker([...COMPOSE, 'restart', 'jellyfin-fake']);
} catch {
  console.log('  no jellyfin-fake container to restart — continuing');
}

for (let i = 0; i < 60; i++) {
  try {
    // `fetch` has no timeout of its own.
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
console.error('the backend did not become healthy in 60 attempts (up to 6 minutes)');
process.exit(1);
