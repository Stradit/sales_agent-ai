#!/usr/bin/env bash
# Phase 5, part 2 — run ON the EC2 host BEFORE the first deploy (the app creates tables on
# start, and this script refuses a non-empty database):
#
#   sudo bash restore_db.sh /tmp/salesai_<date>.dump
#
# Uses the postgres:18 image for pg_restore/psql (no client install), the RDS URL from
# /opt/salesai/.env (passed as an environment variable, never on a command line), restores
# without owners/grants, skips extension entries (RDS: created here instead), then compares
# exact row counts with the .counts.txt written by dump_local_db.sh.
set -euo pipefail

DUMP="$(readlink -f "${1:?usage: restore_db.sh <file.dump>}")"
DIR="$(dirname "$DUMP")"; NAME="$(basename "$DUMP")"
COUNTS="${DUMP%.dump}.counts.txt"
ENV_FILE=/opt/salesai/.env
[[ -f "$DUMP" && -f "$COUNTS" ]] || { echo "Need $NAME and its .counts.txt side by side" >&2; exit 1; }
URL="$(grep -E '^DATABASE_URL=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
[[ -n "$URL" ]] || { echo "DATABASE_URL missing in $ENV_FILE" >&2; exit 1; }
if docker ps --format '{{.Names}}' | grep -qx salesai-web; then
  echo "salesai-web is running — stop it first: docker compose -f /opt/salesai/docker-compose.prod.yml down" >&2; exit 1
fi

pg() { docker run --rm -i --network host -e DBURL="$URL" -v "$DIR":/dump postgres:18 sh -c "$1"; }

COUNT_SQL="select table_name, (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from %I.%I', table_schema, table_name), false, true, '')))[1]::text
           from information_schema.tables where table_schema = 'public' and table_type = 'BASE TABLE' order by 1"

echo "== Target: $(pg 'psql "$DBURL" -Atc "show server_version"')"
n="$(pg 'psql "$DBURL" -Atc "select count(*) from information_schema.tables where table_schema = '\''public'\''"')"
[[ "$n" == "0" ]] || { echo "Target already has $n tables — refusing to restore over it." >&2; exit 1; }

echo "== Extensions"
pg 'psql "$DBURL" -v ON_ERROR_STOP=1 -c "create extension if not exists pg_trgm"'

echo "== Restore $NAME"
pg "pg_restore -l /dump/$NAME | grep -v -E ' EXTENSION | COMMENT - EXTENSION ' > /dump/restore.list"
pg "pg_restore --no-owner --no-privileges --exit-on-error --jobs=2 -L /dump/restore.list --dbname=\"\$DBURL\" /dump/$NAME"
rm -f "$DIR/restore.list"

echo "== Verify row counts"
pg "psql \"\$DBURL\" -At -c \"$COUNT_SQL\"" > "$DIR/target.counts.txt"
if diff <(sort "$COUNTS") <(sort "$DIR/target.counts.txt") > "$DIR/counts.diff"; then
  echo "All $(wc -l < "$COUNTS") tables match row for row."
else
  echo "!! Row counts differ:" >&2; cat "$DIR/counts.diff" >&2; exit 1
fi
pg 'psql "$DBURL" -qc "analyze"'
echo "== Restore complete. Next: run the Deploy workflow, then Admin → Copilot pipeline → Full re-embed."
