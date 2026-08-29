# DDL check without Docker

`test_migrations.py` skips unless this directory has `node_modules`. To enable it:

```bash
npm --prefix backend/tests/pglite install
```

PGlite is a real Postgres compiled to wasm, so DDL that applies here applies in `postgres:16`.
It is a syntax and structure check, not a substitute for running against the real server —
version-specific behaviour still needs `docker compose up db`.
