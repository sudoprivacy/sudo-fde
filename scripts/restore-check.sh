#!/bin/sh
set -eu
: "${FDE_RESTORE_DATABASE_URL:?Provide an isolated, EMPTY restore-test database URL}"
input="${1:?Provide a custom-format backup path}"
count=$(psql "$FDE_RESTORE_DATABASE_URL" -Atc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'")
if [ "$count" != "0" ]; then
  printf 'Refused: restore target is not empty.\n' >&2
  exit 1
fi
pg_restore --exit-on-error --dbname="$FDE_RESTORE_DATABASE_URL" "$input"
psql "$FDE_RESTORE_DATABASE_URL" -v ON_ERROR_STOP=1 -c "SELECT version_num FROM alembic_version" \
  -c "SELECT relname,relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname IN ('workspaces','evidence','knowledge_chunks','router_calls')"
psql "$FDE_RESTORE_DATABASE_URL" -v ON_ERROR_STOP=1 <<'SQL'
DO $$ BEGIN
  IF (SELECT count(*) FROM pg_class WHERE relname IN ('workspaces','evidence','knowledge_chunks','router_calls')
      AND relrowsecurity AND relforcerowsecurity AND relnamespace='public'::regnamespace) <> 4 THEN
    RAISE EXCEPTION 'Restored tenant boundaries are incomplete';
  END IF;
END $$;
SQL
printf 'Database restored. Verify object hashes, runtime-role RLS and Router reconciliation before traffic.\n'
