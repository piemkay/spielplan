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

// Relations alone cannot see an ALTER. 0010 and 0012 both add columns to tables 0005 already
// created, so a migration that applies and adds nothing would pass every assertion above it.
const col = await db.query(`
  SELECT table_schema, table_name, column_name, data_type, is_nullable
    FROM information_schema.columns
   WHERE table_schema IN ('public', 'display', 'review_store')
   ORDER BY 1, 2, 3`);
const idx = await db.query(`
  SELECT schemaname AS table_schema, tablename AS table_name, indexname, indexdef
    FROM pg_indexes
   WHERE schemaname IN ('public', 'display', 'review_store')
   ORDER BY 1, 2, 3`);
console.log(
  JSON.stringify({ ok: true, applied, relations: rel.rows, columns: col.rows, indexes: idx.rows }),
);
await db.close();
