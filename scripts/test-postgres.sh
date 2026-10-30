#!/bin/sh
# Starts only a dedicated, disposable test database; never uses a configured production database.
set -eu
CONTAINER="sudo-fde-test-$$"
PORT="${FDE_TEST_PORT:-15439}"
cleanup() { docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM
docker run -d --name "$CONTAINER" -e POSTGRES_PASSWORD=test-admin-only -e POSTGRES_DB=fde \
  -p "127.0.0.1:$PORT:5432" postgres:16-alpine >/dev/null
i=0
until docker exec "$CONTAINER" pg_isready -h 127.0.0.1 -U postgres -d fde >/dev/null 2>&1; do
  i=$((i+1)); [ "$i" -lt 30 ] || exit 1
  sleep 1
done
docker exec -i "$CONTAINER" psql -U postgres -d fde -v ON_ERROR_STOP=1 <<'SQL'
CREATE ROLE fde_app LOGIN PASSWORD 'test-app-only' NOSUPERUSER NOBYPASSRLS;
GRANT CONNECT ON DATABASE fde TO fde_app;
GRANT USAGE ON SCHEMA public TO fde_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO fde_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO fde_app;
SQL
export FDE_ENVIRONMENT=test
export FDE_DATABASE_URL="postgresql+psycopg://fde_app:test-app-only@127.0.0.1:$PORT/fde"
export FDE_MIGRATION_DATABASE_URL="postgresql+psycopg://postgres:test-admin-only@127.0.0.1:$PORT/fde"
export FDE_TEST_DATABASE_URL="$FDE_DATABASE_URL"
.venv/bin/alembic upgrade head
.venv/bin/pytest -q tests/test_postgres.py
