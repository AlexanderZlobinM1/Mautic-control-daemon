#!/usr/bin/env bash
# Safe InnoDB shrink/compression helper for Mautic databases.
#
# Goals:
# - no data deletion;
# - dry-run by default;
# - apply only when free space checks pass;
# - table-by-table operation to limit temporary space usage.
#
# Typical usage:
#   sudo bash mautic-db-compress-safe.sh --dry-run
#   sudo bash mautic-db-compress-safe.sh --apply --database baza_ss
#
# Exit codes:
#   0  success (or dry-run completed)
#   2  safety check failed (do not run ALTER now)
#   3  invalid arguments / environment issue
set -euo pipefail

MODE="compress"                # compress|rebuild
APPLY=0                        # 0=dry-run, 1=apply
DB_NAME=""
MYSQL_HOST="localhost"
MYSQL_PORT="3306"
MYSQL_USER=""
MYSQL_PASSWORD=""
MYSQL_DEFAULTS_FILE=""
KEY_BLOCK_SIZE="8"
MIN_TABLE_MB="64"
RESERVE_FREE_GB="5"
TEMP_FACTOR="1.35"
WITH_MCD_MAINTENANCE=0
CONTINUE_ON_ERROR=1

MCD_MAINTENANCE_ENABLED=0

log() {
  printf '%s %s\n' "$(date '+%F %T')" "$*"
}

die() {
  log "ERROR: $*"
  exit 3
}

cleanup() {
  if [[ "$MCD_MAINTENANCE_ENABLED" -eq 1 ]]; then
    if command -v mcd-cli >/dev/null 2>&1; then
      log "Disabling MCD maintenance mode..."
      mcd-cli maintenance off >/dev/null 2>&1 || true
    fi
  fi
}
trap cleanup EXIT

usage() {
  cat <<'EOF'
Usage: mautic-db-compress-safe.sh [options]

Options:
  --dry-run                      Analyze only (default)
  --apply                        Execute ALTER/OPTIMIZE (after safety checks)
  --mode compress|rebuild        compress: ROW_FORMAT=COMPRESSED
                                 rebuild : OPTIMIZE TABLE (rebuild+reclaim)
  --database <name>              Database name; if omitted, tries auto-detect from Mautic local.php
  --host <host>                  MySQL host (default: localhost)
  --port <port>                  MySQL port (default: 3306)
  --user <user>                  MySQL user (default: socket auth/current user)
  --password <pass>              MySQL password
  --defaults-file <path>         MySQL option file, e.g. /root/.my.cnf
  --key-block-size <n>           KEY_BLOCK_SIZE for COMPRESSED (default: 8)
  --min-table-mb <mb>            Skip smaller tables (default: 64)
  --reserve-free-gb <gb>         Safety reserve on FS (default: 5)
  --temp-factor <float>          Temp space factor vs biggest table (default: 1.35)
  --with-mcd-maintenance         mcd-cli maintenance on/off around apply
  --stop-on-error                Stop on first table error
  --continue-on-error            Continue on table errors (default)
  -h, --help                     Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) APPLY=0; shift ;;
    --apply) APPLY=1; shift ;;
    --mode) MODE="${2:-}"; shift 2 ;;
    --database) DB_NAME="${2:-}"; shift 2 ;;
    --host) MYSQL_HOST="${2:-}"; shift 2 ;;
    --port) MYSQL_PORT="${2:-}"; shift 2 ;;
    --user) MYSQL_USER="${2:-}"; shift 2 ;;
    --password) MYSQL_PASSWORD="${2:-}"; shift 2 ;;
    --defaults-file) MYSQL_DEFAULTS_FILE="${2:-}"; shift 2 ;;
    --key-block-size) KEY_BLOCK_SIZE="${2:-}"; shift 2 ;;
    --min-table-mb) MIN_TABLE_MB="${2:-}"; shift 2 ;;
    --reserve-free-gb) RESERVE_FREE_GB="${2:-}"; shift 2 ;;
    --temp-factor) TEMP_FACTOR="${2:-}"; shift 2 ;;
    --with-mcd-maintenance) WITH_MCD_MAINTENANCE=1; shift ;;
    --stop-on-error) CONTINUE_ON_ERROR=0; shift ;;
    --continue-on-error) CONTINUE_ON_ERROR=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

[[ "$MODE" == "compress" || "$MODE" == "rebuild" ]] || die "--mode must be compress|rebuild"
[[ "$MIN_TABLE_MB" =~ ^[0-9]+$ ]] || die "--min-table-mb must be integer"
[[ "$RESERVE_FREE_GB" =~ ^[0-9]+$ ]] || die "--reserve-free-gb must be integer"
[[ "$KEY_BLOCK_SIZE" =~ ^[0-9]+$ ]] || die "--key-block-size must be integer"

if command -v mariadb >/dev/null 2>&1; then
  MYSQL_BIN="mariadb"
elif command -v mysql >/dev/null 2>&1; then
  MYSQL_BIN="mysql"
else
  die "mysql/mariadb client is not installed"
fi

MYSQL_CMD=("$MYSQL_BIN" "--batch" "--raw" "--skip-column-names" "--host=$MYSQL_HOST" "--port=$MYSQL_PORT")
if [[ -n "$MYSQL_DEFAULTS_FILE" ]]; then
  MYSQL_CMD+=("--defaults-extra-file=$MYSQL_DEFAULTS_FILE")
fi
if [[ -n "$MYSQL_USER" ]]; then
  MYSQL_CMD+=("--user=$MYSQL_USER")
fi
if [[ -n "$MYSQL_PASSWORD" ]]; then
  MYSQL_CMD+=("--password=$MYSQL_PASSWORD")
fi

sql() {
  local q="$1"
  "${MYSQL_CMD[@]}" -e "$q"
}

sql_db() {
  local db="$1"
  local q="$2"
  "${MYSQL_CMD[@]}" "$db" -e "$q"
}

escape_sql() {
  # single-quote safe
  printf "%s" "$1" | sed "s/'/''/g"
}

detect_db_from_local_php() {
  local dbs=()
  local file db
  while IFS= read -r file; do
    db="$(php -r '
      $p=$argv[1];
      $x=@include $p;
      if (is_array($x) && isset($x["db_name"]) && is_string($x["db_name"])) { echo $x["db_name"]; }
    ' "$file" 2>/dev/null || true)"
    if [[ -n "$db" ]]; then
      dbs+=("$db")
    fi
  done < <(find /var/www -type f \( -path '*/config/local.php' -o -path '*/app/config/local.php' \) 2>/dev/null | sort -u)

  if [[ "${#dbs[@]}" -eq 0 ]]; then
    return 1
  fi
  mapfile -t uniq_dbs < <(printf "%s\n" "${dbs[@]}" | awk 'NF{a[$0]=1} END{for (k in a) print k}' | sort)
  if [[ "${#uniq_dbs[@]}" -ne 1 ]]; then
    log "Auto-detected multiple DBs:"
    printf ' - %s\n' "${uniq_dbs[@]}"
    log "Please pass --database <name>"
    return 2
  fi
  DB_NAME="${uniq_dbs[0]}"
  return 0
}

if [[ -z "$DB_NAME" ]]; then
  log "Database is not specified, trying auto-detect from Mautic local.php..."
  if ! detect_db_from_local_php; then
    die "Could not determine DB name automatically"
  fi
fi

DB_ESC="$(escape_sql "$DB_NAME")"

log "Checking DB connectivity..."
sql "SELECT 1;" >/dev/null

DB_EXISTS="$(sql "SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name='${DB_ESC}'")"
[[ "$DB_EXISTS" == "1" ]] || die "Database not found: $DB_NAME"

DATADIR="$(sql "SELECT @@datadir")"
FILE_PER_TABLE="$(sql "SELECT @@innodb_file_per_table")"
VERSION_INFO="$(sql "SELECT VERSION()")"

log "MySQL version      : $VERSION_INFO"
log "Database           : $DB_NAME"
log "Data dir           : $DATADIR"
log "innodb_file_per_table: $FILE_PER_TABLE"

if [[ "$FILE_PER_TABLE" != "1" ]]; then
  log "SAFETY FAIL: innodb_file_per_table=0. Shrink/reclaim on table files is limited."
  log "Action: enable innodb_file_per_table and migrate, or upgrade disk."
  exit 2
fi

ROOT_FREE_BYTES="$(df -PB1 / | awk 'NR==2{print $4}')"
DATA_FREE_BYTES="$(df -PB1 "$DATADIR" | awk 'NR==2{print $4}')"
ROOT_USED_PCT="$(df -P / | awk 'NR==2{gsub(/%/,"",$5); print $5}')"
DATA_USED_PCT="$(df -P "$DATADIR" | awk 'NR==2{gsub(/%/,"",$5); print $5}')"
RESERVE_BYTES="$(( RESERVE_FREE_GB * 1024 * 1024 * 1024 ))"

log "FS usage /         : ${ROOT_USED_PCT}% (free=$(numfmt --to=iec "$ROOT_FREE_BYTES" 2>/dev/null || echo "$ROOT_FREE_BYTES"))"
log "FS usage datadir   : ${DATA_USED_PCT}% (free=$(numfmt --to=iec "$DATA_FREE_BYTES" 2>/dev/null || echo "$DATA_FREE_BYTES"))"

mapfile -t TABLES < <(
  sql "
    SELECT
      table_name,
      COALESCE(data_length,0)+COALESCE(index_length,0) AS total_bytes,
      COALESCE(data_free,0) AS data_free,
      COALESCE(row_format,'') AS row_format
    FROM information_schema.tables
    WHERE table_schema='${DB_ESC}'
      AND table_type='BASE TABLE'
      AND engine='InnoDB'
    ORDER BY (COALESCE(data_length,0)+COALESCE(index_length,0)) DESC
  "
)

if [[ "${#TABLES[@]}" -eq 0 ]]; then
  log "No InnoDB tables found in $DB_NAME"
  exit 0
fi

MIN_TABLE_BYTES="$(( MIN_TABLE_MB * 1024 * 1024 ))"

declare -a CANDIDATES=()
BIGGEST_BYTES=0
TOTAL_BYTES=0
TOTAL_FREE_BYTES=0

for row in "${TABLES[@]}"; do
  IFS=$'\t' read -r t_name t_size t_free t_rowfmt <<<"$row"
  [[ -n "$t_name" ]] || continue
  [[ "$t_size" =~ ^[0-9]+$ ]] || t_size=0
  [[ "$t_free" =~ ^[0-9]+$ ]] || t_free=0
  TOTAL_BYTES=$(( TOTAL_BYTES + t_size ))
  TOTAL_FREE_BYTES=$(( TOTAL_FREE_BYTES + t_free ))
  (( t_size > BIGGEST_BYTES )) && BIGGEST_BYTES="$t_size"

  if (( t_size < MIN_TABLE_BYTES )); then
    continue
  fi
  if [[ "$MODE" == "compress" ]]; then
    # Skip already compressed tables.
    if [[ "${t_rowfmt,,}" == "compressed" ]]; then
      continue
    fi
  else
    # rebuild mode: if no free space in table, low gain but still allowed.
    :
  fi
  CANDIDATES+=("$row")
done

if [[ "${#CANDIDATES[@]}" -eq 0 ]]; then
  log "No candidate tables (mode=$MODE, min_table_mb=$MIN_TABLE_MB). Nothing to do."
  exit 0
fi

TEMP_NEED_BYTES="$(python3 - <<PY
import math
print(int(math.ceil(${BIGGEST_BYTES} * float(${TEMP_FACTOR}))))
PY
)"

log "Tables total size  : $(numfmt --to=iec "$TOTAL_BYTES" 2>/dev/null || echo "$TOTAL_BYTES")"
log "Tables data_free   : $(numfmt --to=iec "$TOTAL_FREE_BYTES" 2>/dev/null || echo "$TOTAL_FREE_BYTES")"
log "Biggest table size : $(numfmt --to=iec "$BIGGEST_BYTES" 2>/dev/null || echo "$BIGGEST_BYTES")"
log "Temp need estimate : $(numfmt --to=iec "$TEMP_NEED_BYTES" 2>/dev/null || echo "$TEMP_NEED_BYTES") (factor=$TEMP_FACTOR)"
log "Reserve free bytes : $(numfmt --to=iec "$RESERVE_BYTES" 2>/dev/null || echo "$RESERVE_BYTES")"

SAFE=1
if (( DATA_FREE_BYTES < TEMP_NEED_BYTES + RESERVE_BYTES )); then
  SAFE=0
  log "SAFETY FAIL: datadir fs free space is not enough for temp rebuild + reserve."
fi
if (( ROOT_FREE_BYTES < TEMP_NEED_BYTES + RESERVE_BYTES )); then
  SAFE=0
  log "SAFETY FAIL: root fs free space is not enough for temp rebuild + reserve."
fi

log "Candidates (${#CANDIDATES[@]}):"
for row in "${CANDIDATES[@]}"; do
  IFS=$'\t' read -r t_name t_size t_free t_rowfmt <<<"$row"
  log " - ${t_name} size=$(numfmt --to=iec "$t_size" 2>/dev/null || echo "$t_size"), free=$(numfmt --to=iec "$t_free" 2>/dev/null || echo "$t_free"), row_format=${t_rowfmt:-unknown}"
done

if [[ "$APPLY" -eq 0 ]]; then
  if [[ "$SAFE" -eq 1 ]]; then
    log "DRY-RUN RESULT: SAFE_TO_APPLY=yes"
    exit 0
  fi
  log "DRY-RUN RESULT: SAFE_TO_APPLY=no (likely needs more disk space / disk upgrade)"
  exit 2
fi

if [[ "$SAFE" -ne 1 ]]; then
  log "APPLY ABORTED: safety checks failed. No changes made."
  exit 2
fi

if [[ "$WITH_MCD_MAINTENANCE" -eq 1 ]]; then
  if command -v mcd-cli >/dev/null 2>&1; then
    log "Enabling MCD maintenance mode..."
    mcd-cli maintenance on --no-kill-running >/dev/null
    MCD_MAINTENANCE_ENABLED=1
  else
    log "mcd-cli not found, maintenance mode skipped"
  fi
fi

log "APPLY MODE: starting table-by-table $MODE..."

OK_CNT=0
FAIL_CNT=0
SAVED_BYTES=0

for row in "${CANDIDATES[@]}"; do
  IFS=$'\t' read -r t_name t_size_before _t_free _t_rowfmt <<<"$row"
  t_esc="$(printf "%s" "$t_name" | sed 's/`/``/g')"

  # Re-check free space before each table.
  ROOT_FREE_BYTES="$(df -PB1 / | awk 'NR==2{print $4}')"
  DATA_FREE_BYTES="$(df -PB1 "$DATADIR" | awk 'NR==2{print $4}')"
  if (( DATA_FREE_BYTES < TEMP_NEED_BYTES + RESERVE_BYTES || ROOT_FREE_BYTES < TEMP_NEED_BYTES + RESERVE_BYTES )); then
    log "STOP: free space dropped below safety limit before table $t_name"
    break
  fi

  if [[ "$MODE" == "compress" ]]; then
    STMT="ALTER TABLE \`${DB_NAME}\`.\`${t_esc}\` ROW_FORMAT=COMPRESSED KEY_BLOCK_SIZE=${KEY_BLOCK_SIZE}"
  else
    STMT="OPTIMIZE TABLE \`${DB_NAME}\`.\`${t_esc}\`"
  fi

  log "Running: $STMT"
  set +e
  sql_db "$DB_NAME" "$STMT;"
  rc=$?
  set -e
  if [[ "$rc" -ne 0 ]]; then
    FAIL_CNT=$(( FAIL_CNT + 1 ))
    log "FAILED table=$t_name rc=$rc"
    if [[ "$CONTINUE_ON_ERROR" -eq 0 ]]; then
      break
    fi
    continue
  fi

  # Analyze for better stats/plans.
  set +e
  sql_db "$DB_NAME" "ANALYZE TABLE \`${t_esc}\`;" >/dev/null 2>&1
  set -e

  t_size_after="$(sql "SELECT COALESCE(data_length,0)+COALESCE(index_length,0) FROM information_schema.tables WHERE table_schema='${DB_ESC}' AND table_name='$(escape_sql "$t_name")'")"
  [[ "$t_size_after" =~ ^[0-9]+$ ]] || t_size_after="$t_size_before"
  delta=$(( t_size_before - t_size_after ))
  if (( delta > 0 )); then
    SAVED_BYTES=$(( SAVED_BYTES + delta ))
  fi

  OK_CNT=$(( OK_CNT + 1 ))
  log "OK table=$t_name before=$(numfmt --to=iec "$t_size_before" 2>/dev/null || echo "$t_size_before") after=$(numfmt --to=iec "$t_size_after" 2>/dev/null || echo "$t_size_after") delta=$(numfmt --to=iec "$delta" 2>/dev/null || echo "$delta")"
done

log "DONE: ok=$OK_CNT failed=$FAIL_CNT estimated_saved=$(numfmt --to=iec "$SAVED_BYTES" 2>/dev/null || echo "$SAVED_BYTES")"

if [[ "$FAIL_CNT" -gt 0 ]]; then
  exit 2
fi
exit 0

