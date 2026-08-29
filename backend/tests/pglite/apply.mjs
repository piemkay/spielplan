// Apply every migration to a throwaway in-process Postgres and print the resulting relations
// as JSON. Used by tests/test_migrations.py; PGlite is a real Postgres build (wasm), so a
// migration that parses here parses in the postgres:16 container.
import { PGlite } from '@electric-sql/pglite';
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';

const dir = process.argv[2];
const files = readdirSync(dir).filter((f) => f.endsWith('.sql')).sort();
const db = await PGlite.create();
const applied = [];

for (const f of files) {
  try {
    await db.exec(readFileSync(join(dir, f), 'utf8'));
    applied.push(f);
  } catch (e) {
    console.log(JSON.stringify({ ok: false, file: f, error: e.message }));
    await db.close();
    process.exit(1);
  }
}

const rel = await db.query(`
  SELECT table_schema, table_name, table_type
    FROM information_schema.tables
   WHERE table_schema IN ('public', 'display', 'review_store')
   ORDER BY 1, 2`);
console.log(JSON.stringify({ ok: true, applied, relations: rel.rows }));
await db.close();
