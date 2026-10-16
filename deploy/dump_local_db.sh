#!/usr/bin/env bash
# Phase 5, part 1 — run on the PC that hosts today's database (Git Bash), from the repo root:
#
#   bash deploy/dump_local_db.sh            # → salesai_<date>.dump + salesai_<date>.counts.txt
#
# Stop the local app first so nothing writes during the dump. Reads DATABASE_URL from .env
# without printing it. Custom-format dump, no owners/grants (RDS has its own users), without
# the unused "vector" extension. Also writes exact per-table row counts that restore_db.sh
# compares against after the restore.
set -euo pipefail

PG_BIN="${PG_BIN:-/c/Program Files/PostgreSQL/18/bin}"
STAMP="$(date +%Y%m%d_%H%M)"
OUT="${1:-salesai_${STAMP}.dump}"
COUNTS="${OUT%.dump}.counts.txt"

[[ -f .env ]] || { echo "Run from the repo root (no .env here)." >&2; exit 1; }
URL="$(grep -E '^DATABASE_URL=' .env | head -1 | cut -d= -f2- | sed -E 's/^["'\'']|["'\'']$//g')"
[[ -n "$URL" ]] || { echo "DATABASE_URL not found in .env" >&2; exit 1; }
PGDUMP="$PG_BIN/pg_dump"; PSQL="$PG_BIN/psql"
[[ -x "$PGDUMP" || -x "$PGDUMP.exe" ]] || { echo "pg_dump not found in $PG_BIN (set PG_BIN)" >&2; exit 1; }

COUNT_SQL="select table_name, (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from %I.%I', table_schema, table_name), false, true, '')))[1]::text
           from information_schema.tables where table_schema = 'public' and table_type = 'BASE TABLE' order by 1"

echo "== Server: $("$PSQL" "$URL" -Atc 'show server_version') · size: $("$PSQL" "$URL" -Atc 'select pg_size_pretty(pg_database_size(current_database()))')"
echo "== Dumping to $OUT"
"$PGDUMP" --format=custom --no-owner --no-privileges --exclude-extension=vector \
          --dbname="$URL" --file="$OUT"
"$PSQL" "$URL" -At -c "$COUNT_SQL" > "$COUNTS"

echo "== Done: $(du -h "$OUT" | cut -f1) dump, $(wc -l < "$COUNTS") tables counted"
echo "Upload both files:  scp -i salesai-admin.pem $OUT $COUNTS ubuntu@<EC2_HOST>:/tmp/"
