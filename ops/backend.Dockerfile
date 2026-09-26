# Spielplan backend + worker (same codebase, different entrypoint).
# CPU-only by construction: torch CPU wheels only, no CUDA, builds on a GPU-less VM.

# ---- stage 1: the SvelteKit PWA (static build, served by the backend) ----
FROM node:22-slim AS frontend
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci || npm install
COPY frontend/ ./
RUN npm run build

# ---- stage 2: python runtime ----
FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_SYSTEM_PYTHON=1 \
    SPIELPLAN_STATIC_DIR=/app/static

# The worker runs pg_dump itself (§2) instead of driving `db` through the Docker socket.
# Client pinned to 16 from PGDG: pg_dump refuses a newer server, and trixie ships 17.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 curl ca-certificates gnupg \
 && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
      | gpg --dearmor -o /usr/share/keyrings/pgdg.gpg \
 && . /etc/os-release \
 && echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg]" \
         "https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client-16 \
 && apt-get purge -y --auto-remove gnupg \
 && rm -rf /var/lib/apt/lists/*

# Pinned: it runs as root and installs everything else.
RUN pip install uv==0.11.21

WORKDIR /app
COPY backend/pyproject.toml backend/README.md ./
# The CPU-only torch index comes from pyproject's `[[tool.uv.index]]`, not from a flag here.
RUN uv pip install --system -r pyproject.toml

COPY backend/spielplan ./spielplan
COPY backend/migrations ./migrations
COPY --from=frontend /app/build ./static

# The console scripts. Editable so `db/migrate.MIGRATIONS_DIR` resolves to /app/migrations (a
# wheel carries none); --no-deps so a second resolution cannot move a pin.
RUN uv pip install --system --no-deps -e .

# uid/gid fixed at 1000: ./data is host bind mounts that README and CI chown to this number.
# /app stays root-owned so the runtime user cannot rewrite code, migrations or the served bundle.
RUN groupadd --system --gid 1000 spielplan \
 && useradd --system --uid 1000 --gid 1000 --home-dir /app --shell /usr/sbin/nologin spielplan

USER spielplan

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=5 \
  CMD curl -fsS http://127.0.0.1:8080/api/health || exit 1

CMD ["uvicorn", "spielplan.app:app", "--host", "0.0.0.0", "--port", "8080"]
