#!/bin/sh
set -eu
BASE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$BASE"
COMPOSE="docker-compose --env-file .env.production -f compose.production.yaml"
[ -s .env.production ] || { echo "missing .env.production" >&2; exit 1; }
image=$(awk -F= '/^FDE_IMAGE=/{print substr($0,index($0,"=")+1); exit}' .env.production)
[ -n "$image" ] || { echo "FDE_IMAGE is required" >&2; exit 1; }
docker image inspect "$image" >/dev/null
$COMPOSE config -q
$COMPOSE up -d postgres minio clamav
$COMPOSE run --rm migrate
$COMPOSE run --rm storage-init
$COMPOSE up -d --force-recreate web pattern worker maintenance proxy
$COMPOSE ps
