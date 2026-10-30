#!/bin/sh
set -eu
: "${FDE_BACKUP_DATABASE_URL:?Provide the backup-role PostgreSQL URL}"
output="${1:?Provide an encrypted backup staging path}"
umask 077
pg_dump --dbname="$FDE_BACKUP_DATABASE_URL" --format=custom --file="$output"
pg_restore --list "$output" > "$output.manifest"
printf 'Created database backup and manifest. Apply your encrypted offsite retention policy.\n'
