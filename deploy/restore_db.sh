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
# The app accepts SQLAlchemy's "postgresql+psycopg2://"; psql / pg_restore need plain "postgresql://"
URL="$(printf '%s' "$URL" | sed -E 's#^postgres(ql)?(\+[a-z0-9]+)?://#postgresql://#')"
DBNAME="$(printf '%s' "$URL" | sed -E 's#^[a-z]+://[^/]+/([^?]*).*#\1#')"
ADMIN_URL="$(printf '%s' "$URL" | sed -E 's#^([a-z]+://[^/]+/)[^?]*#\1postgres#')"   # same server, the default db
[[ -n "$DBNAME" && "$DBNAME" != "$URL" ]] || { echo "DATABASE_URL has no database name (…:5432/<name>)" >&2; exit 1; }
if docker ps --format '{{.Names}}' | grep -qx salesai-web; then
  echo "salesai-web is running — stop it first: docker compose -f /opt/salesai/docker-compose.prod.yml down" >&2; exit 1
fi

pg() { docker run --rm -i --network host -e DBURL="$URL" -e ADMINURL="$ADMIN_URL" -e DBNAME="$DBNAME" \
       -v "$DIR":/dump postgres:18 sh -c "$1"; }

# Reach the server first (via the default "postgres" db), then make sure the target db exists.
# Errors are summarised without echoing the URL (psql's own messages can include the password).
if ! out="$(pg 'psql "$ADMINURL" -Atc "select 1"' 2>&1)"; then
  case "$out" in
    *"password authentication failed"*) echo "RDS refused the user/password in DATABASE_URL." >&2;;
    *"timeout"*|*"Connection timed out"*|*"could not connect"*|*"Connection refused"*)
      echo "Can't reach RDS on port 5432 — its security group must allow this instance's security group." >&2;;
    *"no pg_hba.conf entry"*) echo "RDS requires SSL — add ?sslmode=require to DATABASE_URL." >&2;;
    *) echo "Can't connect to RDS: $(printf '%s' "$out" | sed -E 's#[a-z]+://[^ "]*#<url>#g' | tail -2)" >&2;;
  esac
  exit 1
fi
if [[ "$(pg 'psql "$ADMINURL" -Atc "select 1 from pg_database where datname = '"'"'$DBNAME'"'"'"' 2>/dev/null)" != "1" ]]; then
  echo "== Database \"$DBNAME\" doesn't exist on RDS yet — creating it"
  pg 'psql "$ADMINURL" -v ON_ERROR_STOP=1 -qc "create database \"$DBNAME\""'
fi

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
