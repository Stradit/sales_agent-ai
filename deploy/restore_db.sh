#!/usr/bin/env bash
# Restore a pg_dump (custom format) into the production database — run ON the EC2 host:
#
#   sudo bash restore_db.sh <file.dump>                       # into an EMPTY database (first time)
#   sudo bash restore_db.sh <file.dump> --replace             # wipe the database first (asks you to type its name)
#   sudo bash restore_db.sh <file.dump> --fix-orphans         # repair rows that point at missing rows (see below)
#
# Uses the postgres:18 image for pg_restore/psql (no client install) and the database URL from
# /opt/salesai/.env (passed as an environment variable, never on a command line). Steps:
#   1. data, tables and indexes — without foreign keys, owners, grants, extensions or "CREATE SCHEMA public"
#   2. check every foreign key for orphans (rows whose referenced row isn't in the dump). Without
#      --fix-orphans the database is emptied again and the orphans are listed; with it, each orphan
#      is resolved the way that foreign key's ON DELETE rule would (SET NULL -> cleared, CASCADE ->
#      row removed); a NO ACTION/RESTRICT orphan always stops the restore.
#   3. foreign keys added (PostgreSQL validates every row)
#   4. exact row counts compared with <file>.counts.txt, if it exists (written by dump_local_db.sh)
# The app container must be stopped first; the next deploy starts it again.
set -euo pipefail

usage() { echo "usage: restore_db.sh <file.dump> [--replace] [--fix-orphans]" >&2; exit 1; }
[[ $# -ge 1 ]] || usage
DUMP="$(readlink -f "$1")"; shift
REPLACE=0; FIX=0
for a in "$@"; do case "$a" in --replace) REPLACE=1;; --fix-orphans) FIX=1;; *) usage;; esac; done
DIR="$(dirname "$DUMP")"; NAME="$(basename "$DUMP")"
COUNTS="${DUMP%.dump}.counts.txt"
ENV_FILE="${RESTORE_ENV_FILE:-/opt/salesai/.env}"
[[ -f "$DUMP" ]] || { echo "No such file: $DUMP" >&2; exit 1; }
[[ "$NAME" != *"'"* ]] || { echo "Rename the dump so its name has no ' in it." >&2; exit 1; }
URL="$(grep -E '^DATABASE_URL=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '\r')"
[[ -n "$URL" ]] || { echo "DATABASE_URL missing in $ENV_FILE" >&2; exit 1; }
# The app accepts SQLAlchemy's "postgresql+psycopg2://"; psql / pg_restore need plain "postgresql://"
URL="$(printf '%s' "$URL" | sed -E 's#^postgres(ql)?(\+[a-z0-9]+)?://#postgresql://#')"
DBNAME="$(printf '%s' "$URL" | sed -E 's#^[a-z]+://[^/]+/([^?]*).*#\1#')"
ADMIN_URL="$(printf '%s' "$URL" | sed -E 's#^([a-z]+://[^/]+/)[^?]*#\1postgres#')"   # same server, the default db
[[ -n "$DBNAME" && "$DBNAME" != "$URL" ]] || { echo "DATABASE_URL has no database name (…:5432/<name>)" >&2; exit 1; }
if command -v docker >/dev/null && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx salesai-web; then
  echo "salesai-web is running — stop it first (the next deploy recreates it): sudo docker rm -f salesai-web" >&2; exit 1
fi

WORK="$(mktemp -d "$DIR/.restore.XXXXXX")"; trap 'rm -rf "$WORK"' EXIT
W="$(basename "$WORK")"
if [[ -n "${RESTORE_LOCAL_PG_BIN:-}" ]]; then
  # Test hook: run the same commands with local client tools instead of the postgres:18 image
  pg() { local c="${1//\/dump\//$DIR/}"; DBURL="$URL" ADMINURL="$ADMIN_URL" DBNAME="$DBNAME" PATH="$RESTORE_LOCAL_PG_BIN:$PATH" sh -c "$c"; }
else
  pg() { docker run --rm -i --network host -e DBURL="$URL" -e ADMINURL="$ADMIN_URL" -e DBNAME="$DBNAME" \
         -v "$DIR":/dump postgres:18 sh -c "$1"; }
fi
q() { pg "psql \"\$DBURL\" -v ON_ERROR_STOP=1 -Atqc \"$1\""; }      # one query on the target db

# Reach the server first (via the default "postgres" db), then make sure the target db exists.
# Errors are summarised without echoing the URL (psql's own messages can include the password).
if ! out="$(pg 'psql "$ADMINURL" -Atc "select 1"' 2>&1)"; then
  case "$out" in
    *"password authentication failed"*) echo "RDS refused the user/password in DATABASE_URL." >&2;;
    *"timeout"*|*"timed out"*|*"could not connect"*|*"Connection refused"*)
      echo "Can't reach RDS on port 5432 — its security group must allow this instance's security group." >&2;;
    *"no pg_hba.conf entry"*) echo "RDS requires SSL — add ?sslmode=require to DATABASE_URL." >&2;;
    *) echo "Can't connect to RDS: $(printf '%s' "$out" | sed -E 's#[a-z]+://[^ "]*#<url>#g' | tail -2)" >&2;;
  esac
  exit 1
fi
recreate_db() { pg 'psql "$ADMINURL" -v ON_ERROR_STOP=1 -qc "drop database if exists \"$DBNAME\" with (force)" -c "create database \"$DBNAME\""'; }
if [[ "$(pg 'psql "$ADMINURL" -Atc "select 1 from pg_database where datname = '"'"'$DBNAME'"'"'"' 2>/dev/null)" != "1" ]]; then
  echo "== Database \"$DBNAME\" doesn't exist yet — creating it"
  pg 'psql "$ADMINURL" -v ON_ERROR_STOP=1 -qc "create database \"$DBNAME\""'
fi

echo "== Target: PostgreSQL $(q 'show server_version') · database $DBNAME"
n="$(q "select count(*) from information_schema.tables where table_schema = 'public'")"
if [[ "$n" != "0" ]]; then
  if [[ "$REPLACE" != 1 ]]; then
    echo "Target already has $n tables — refusing to restore over it. Re-run with --replace to wipe it first." >&2; exit 1
  fi
  echo "!! --replace: database \"$DBNAME\" has $n tables. ALL its data will be deleted and replaced by $NAME."
  echo "   Take an RDS snapshot first if you may need the current data (RDS → Snapshots → Take snapshot)."
  read -rp "   Type the database name to confirm: " answer </dev/tty
  [[ "$answer" == "$DBNAME" ]] || { echo "Not confirmed — nothing changed." >&2; exit 1; }
  recreate_db; echo "== Emptied $DBNAME"
fi

echo "== Extensions"
q "create extension if not exists pg_trgm" >/dev/null

echo "== Reading $NAME"
pg "pg_restore -l '/dump/$NAME'" | tr -d '\r' > "$WORK/full.list"
grep -v -E ' EXTENSION | COMMENT - EXTENSION | SCHEMA - public | COMMENT - SCHEMA public ' "$WORK/full.list" > "$WORK/base.list"
grep -v ' FK CONSTRAINT ' "$WORK/base.list" > "$WORK/data.list"
grep ' FK CONSTRAINT ' "$WORK/base.list" > "$WORK/fk.list" || true
echo "   $(grep -c ' TABLE DATA ' "$WORK/data.list") tables with data · $(wc -l < "$WORK/fk.list" | tr -d ' ') foreign keys"

echo "== 1/4 Restore data, tables and indexes (foreign keys come after the orphan check)"
pg "pg_restore --no-owner --no-privileges --exit-on-error --jobs=2 -L /dump/$W/data.list --dbname=\"\$DBURL\" '/dump/$NAME'"

echo "== 2/4 Check foreign keys for orphans"
pg "pg_restore -L /dump/$W/fk.list -f - '/dump/$NAME'" | tr -d '\r' > "$WORK/fk.sql"
# one line per FK: table|constraint|cols|reftable|refcols|on_delete
awk '/^ALTER TABLE ONLY / {t=$4; sub(/^public\./,"",t); next}
     /ADD CONSTRAINT .* FOREIGN KEY/ {
       line=$0; c=line; sub(/.*ADD CONSTRAINT /,"",c); sub(/ .*/,"",c)
       cols=line; sub(/.*FOREIGN KEY \(/,"",cols); sub(/\).*/,"",cols)
       rt=line; sub(/.*REFERENCES /,"",rt); sub(/\(.*/,"",rt); sub(/^public\./,"",rt)
       rc=line; sub(/.*REFERENCES [^(]*\(/,"",rc); sub(/\).*/,"",rc)
       od="NO ACTION"; if (line ~ /ON DELETE SET NULL/) od="SET NULL"; else if (line ~ /ON DELETE CASCADE/) od="CASCADE"; else if (line ~ /ON DELETE SET DEFAULT/) od="SET DEFAULT"; else if (line ~ /ON DELETE RESTRICT/) od="RESTRICT"
       gsub(/ /,"",cols); gsub(/ /,"",rc); print t "|" c "|" cols "|" rt "|" rc "|" od }' "$WORK/fk.sql" > "$WORK/fks.txt"
orphan_where() {  # $1 table cols, $2 reftable, $3 ref cols → WHERE clause for orphans in x
  local IFS=,; local a=($1) b=($3) nn="" eq=""; local i
  for i in "${!a[@]}"; do nn+="${nn:+ and }x.${a[$i]} is not null"; eq+="${eq:+ and }y.${b[$i]} = x.${a[$i]}"; done
  printf '%s and not exists (select 1 from public.%s y where %s)' "$nn" "$2" "$eq"
}
: > "$WORK/fixed.txt"; total=0
for pass in 1 2 3 4 5; do
  found=0
  while IFS='|' read -r t c cols rt rc od; do
    [[ -n "$t" ]] || continue
    w="$(orphan_where "$cols" "$rt" "$rc")"
    k="$(q "select count(*) from public.$t x where $w")"
    [[ "$k" == "0" ]] && continue
    found=1
    ids="$(q "select string_agg(v, ',') from (select distinct (x.${cols%%,*})::text v from public.$t x where $w order by 1 limit 10) s")"
    echo "   $k row(s) in $t.$cols point at missing $rt.$rc ($ids) — rule: ON DELETE $od"
    if [[ "$FIX" != 1 ]]; then continue; fi
    case "$od" in
      "SET NULL") set_list="$(echo "$cols" | sed -E 's/([a-z0-9_]+)/\1 = null/g')"
                  q "update public.$t x set $set_list where $w" >/dev/null; echo "      fixed: cleared $cols";;
      CASCADE)    q "delete from public.$t x where $w" >/dev/null; echo "      fixed: removed those rows"
                  echo "$t|$k" >> "$WORK/fixed.txt";;
      *)          echo "      can't resolve automatically (ON DELETE $od) — fix the source data and dump again" >&2; exit 1;;
    esac
    total=$((total + k))
  done < "$WORK/fks.txt"
  [[ "$found" == 0 ]] && break
  if [[ "$FIX" != 1 ]]; then
    echo "!! Orphaned rows found (listed above). Emptying $DBNAME again so nothing half-restored is left." >&2
    recreate_db
    echo "   Re-run with --fix-orphans to resolve them by their ON DELETE rules, or fix the source and dump again." >&2
    exit 1
  fi
done
[[ "$total" == 0 ]] && echo "   none" || echo "   resolved $total orphan row(s)"

echo "== 3/4 Add foreign keys"
pg "pg_restore --no-owner --no-privileges --exit-on-error -L /dump/$W/fk.list --dbname=\"\$DBURL\" '/dump/$NAME'"

echo "== 4/4 Verify row counts"
COUNT_SQL="select table_name, (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from %I.%I', table_schema, table_name), false, true, '')))[1]::text from information_schema.tables where table_schema = 'public' and table_type = 'BASE TABLE' order by 1"
q "$COUNT_SQL" | tr -d '\r' > "$WORK/target.counts"
if [[ -f "$COUNTS" ]]; then
  tr -d '\r' < "$COUNTS" | sort > "$WORK/source.counts"; sort "$WORK/target.counts" -o "$WORK/target.counts"
  bad=0
  while IFS='|' read -r t s; do
    d="$(grep -E "^$t\|" "$WORK/target.counts" | cut -d'|' -f2)"
    [[ "$d" == "$s" ]] && continue
    removed="$(awk -F'|' -v t="$t" '$1==t {r+=$2} END{print r+0}' "$WORK/fixed.txt")"
    if [[ -n "$d" && $((s - d)) -eq "$removed" ]]; then echo "   $t: $d of $s rows ($removed orphan row(s) removed, as expected)"
    else echo "!! $t: source $s rows, restored ${d:-none}" >&2; bad=1; fi
  done < "$WORK/source.counts"
  [[ "$bad" == 0 ]] || { echo "Row counts differ — see above." >&2; exit 1; }
  if [[ -s "$WORK/fixed.txt" ]]; then echo "   Every other table matches the source row for row."; else echo "   All $(wc -l < "$WORK/source.counts" | tr -d ' ') tables match the source row for row."; fi
else
  echo "   (no $(basename "$COUNTS") next to the dump — showing restored totals instead)"
  echo "   $(wc -l < "$WORK/target.counts" | tr -d ' ') tables, $(awk -F'|' '{s+=$2} END{print s}' "$WORK/target.counts") rows"
fi
q "analyze" >/dev/null
REV="$(q "select version_num from alembic_version" 2>/dev/null || true)"
echo "== Restore complete."
if [[ -n "$REV" ]]; then
  echo "   Schema version $REV (under migration control). The next deploy applies any newer migrations."
else
  echo "   This database is not under migration control yet (the dump came from an unstamped database)."
  echo "   After the next deploy is healthy, stamp it once (verifies first, changes nothing else):"
  echo "     docker exec salesai-web python -m db.migrate stamp-baseline        # check"
  echo "     docker exec salesai-web python -m db.migrate stamp-baseline --yes  # stamp"
fi
echo "   Then: Admin → Copilot pipeline → Full re-embed."
