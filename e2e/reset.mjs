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
import { existsSync, readFileSync, rmSync } from 'node:fs';
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
rmSync(join(ROOT, 'data', 'artifacts'), { recursive: true, force: true });

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
