#!/bin/sh
set -eu
BASE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$BASE"
COMPOSE="docker-compose --env-file .env.production -f compose.production.yaml"
[ -s .env.production ] || { echo "missing .env.production" >&2; exit 1; }
web_image=$(awk -F= '/^FDE_WEB_IMAGE=/{print substr($0,index($0,"=")+1); exit}' .env.production)
server_image=$(awk -F= '/^FDE_SERVER_IMAGE=/{print substr($0,index($0,"=")+1); exit}' .env.production)
[ -n "$web_image" ] || { echo "FDE_WEB_IMAGE is required" >&2; exit 1; }
[ -n "$server_image" ] || { echo "FDE_SERVER_IMAGE is required" >&2; exit 1; }
docker image inspect "$web_image" >/dev/null
docker image inspect "$server_image" >/dev/null
$COMPOSE config -q
$COMPOSE up -d postgres minio
$COMPOSE run --rm migrate
$COMPOSE run --rm storage-init
$COMPOSE up -d --force-recreate --remove-orphans web server
$COMPOSE ps
