# Spielplan backend + worker (same codebase, different entrypoint) — spec v2.1 §1, §2.
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

# §2: "nightly pg_dump to /data/backups, rotation 14". The worker runs the binary itself rather
# than driving the `db` service, which would need the Docker socket inside a container whose
# stored connector secret is already admin-equivalent (§14.3) — and would have nothing to drive
# on an install whose DATABASE_URL points at a Postgres outside this compose file.
#
# Pinned to 16, from PGDG, because §1 pins the server to 16 and Debian trixie ships 17 in main:
# pg_dump refuses to dump a server newer than itself, and a dump taken by a newer client is one
# the household's own Postgres may not be able to read back. A client that is one major off in
# either direction is not a degraded backup, it is no backup.
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

RUN pip install uv

WORKDIR /app
COPY backend/pyproject.toml backend/README.md ./
# torch (added at M2 for the Cold Tower) resolves from the CPU-only index;
# nothing here may drag in a CUDA wheel.
RUN uv pip install --system --index-strategy unsafe-best-match \
      --extra-index-url https://download.pytorch.org/whl/cpu \
      -r pyproject.toml

COPY backend/spielplan ./spielplan
COPY backend/migrations ./migrations
COPY --from=frontend /app/build ./static

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=5 \
  CMD curl -fsS http://127.0.0.1:8080/api/health || exit 1

CMD ["uvicorn", "spielplan.app:app", "--host", "0.0.0.0", "--port", "8080"]
