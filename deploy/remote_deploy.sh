#!/usr/bin/env bash
# Runs ON the EC2 host (as "deploy"), called by GitHub Actions over SSH:
#
#   bash /opt/salesai/remote_deploy.sh <ecr-image-uri>
#
# 1. pull the image
# 2. database migrations (docs/DB_DEPLOYMENT_PLAN.md), in a one-off container of the NEW image,
#    while the old app keeps serving:  check -> RDS snapshot -> `python -m db.migrate upgrade`.
#    Migrations are backward-compatible (expand/contract), so the old app runs fine on the new
#    schema, and a failed migration stops the deploy before the app is touched.
# 3. start the new app, wait for /api/health (which fails if the schema is behind the code), and
#    roll back to the previous IMAGE if it isn't healthy. The schema is never rolled back - if a
#    migration itself was wrong, restore the snapshot taken in step 2 (deploy/RESTORE.md).
#
# Optional settings in /opt/salesai/.env:
#   RDS_INSTANCE_ID=salesai-db     snapshot this RDS instance before applying migrations
#                                  (instance role needs rds:CreateDBSnapshot + rds:DescribeDBSnapshots)
#   SKIP_PRE_MIGRATION_SNAPSHOT=1  migrate without a snapshot (not recommended)
set -euo pipefail

IMAGE="${1:?usage: remote_deploy.sh <ecr-image-uri>}"
APP_DIR="${SALESAI_APP_DIR:-/opt/salesai}"   # override only for tests
COMPOSE="docker compose -f $APP_DIR/docker-compose.prod.yml"
REGION="${AWS_REGION:-us-east-1}"
REGISTRY="${IMAGE%%/*}"
HEALTH_URL=http://127.0.0.1:8000/api/health
WAIT_SECONDS=180

[[ -f "$APP_DIR/.env" ]] || { echo "Missing $APP_DIR/.env" >&2; exit 1; }

echo "== ECR login ($REGISTRY)"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY"

PREVIOUS="$(docker inspect --format '{{.Config.Image}}' salesai-web 2>/dev/null || true)"
echo "Previous image: ${PREVIOUS:-none}"

# ── Disk: keep only what a rollback needs ────────────────────────────────────
# Each app image is several GB (the embedding model is baked in). Before pulling, delete every
# app image except the one running now (the rollback target), plus stopped containers, dangling
# layers and build cache. Then make sure the pull fits, instead of dying half-way through it.
MIN_FREE_GB="${MIN_FREE_GB:-5}"
DOCKER_ROOT="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)"
free_gb() { df -P -k "$DOCKER_ROOT" 2>/dev/null | awk 'NR==2 {printf "%d", $4 / 1048576}'; }
prune_images() {      # args: images to keep (repo:tag); every other image of this repository goes
  local repo="${IMAGE%:*}" keep img
  docker container prune -f >/dev/null 2>&1 || true
  while read -r img; do
    [[ -z "$img" || "$img" == *"<none>"* ]] && continue
    for keep in "$@"; do [[ "$img" == "$keep" ]] && continue 2; done
    docker image rm "$img" >/dev/null 2>&1 && echo "   removed $img" || true
  done < <(docker images --format '{{.Repository}}:{{.Tag}}' "$repo" 2>/dev/null)
  docker image prune -f >/dev/null 2>&1 || true          # dangling layers
  docker builder prune -af >/dev/null 2>&1 || true       # build cache (nothing is built here)
}

echo "== Disk before pull: $(free_gb) GB free in $DOCKER_ROOT"
prune_images "$PREVIOUS" "$IMAGE"
FREE="$(free_gb)"
echo "   after clean-up: ${FREE} GB free"
if [[ -n "$FREE" ]] && (( FREE < MIN_FREE_GB )); then
  echo "!! Only ${FREE} GB free - the new image needs about ${MIN_FREE_GB} GB. Nothing was changed; the" >&2
  echo "   previous version is still serving. Grow the server disk (deploy/AWS_SETUP.md, 'Disk')." >&2
  df -h "$DOCKER_ROOT" / >&2 || true
  exit 1
fi

echo "== Pull $IMAGE"
docker pull "$IMAGE"

# .env as the app reads it (python-dotenv / compose are lenient: "KEY = value", quotes, "export ",
# CRLF). `docker run --env-file` is strict and passes quotes through literally, so the one-off
# migration container gets a cleaned copy: KEY=value per line, comments and blanks dropped.
ENV_CLEAN="$APP_DIR/.env.migrate"
clean_env() {
  ( umask 077
    sed -e 's/\r$//' -e '/^[[:space:]]*#/d' -e '/^[[:space:]]*$/d' -e 's/^[[:space:]]*export[[:space:]]\+//' \
        -e 's/^[[:space:]]*\([A-Za-z_][A-Za-z0-9_]*\)[[:space:]]*=[[:space:]]*/\1=/' \
        -e 's/^\([A-Za-z_][A-Za-z0-9_]*\)="\(.*\)"[[:space:]]*$/\1=\2/' \
        -e "s/^\([A-Za-z_][A-Za-z0-9_]*\)='\(.*\)'[[:space:]]*\$/\1=\2/" \
        "$APP_DIR/.env" | grep -E '^[A-Za-z_][A-Za-z0-9_]*=' > "$ENV_CLEAN" )
}
trap 'rm -f "$ENV_CLEAN"' EXIT
env_value() { grep -E "^$1=" "$ENV_CLEAN" | tail -1 | cut -d= -f2- || true; }
migrate() { docker run --rm --env-file "$ENV_CLEAN" -e CRM_BACKGROUND=0 -e COPILOT_AUTOSYNC=0 \
              --entrypoint python "$IMAGE" -m db.migrate "$@"; }

echo "== Database migrations"
clean_env
set +e; migrate check; CHECK_RC=$?; set -e
if (( CHECK_RC == 0 )); then
  echo "Schema is up to date (or not yet under migration control) - nothing to apply."
elif (( CHECK_RC != 1 )); then
  # 1 means "migrations pending"; anything else means the check itself couldn't run
  echo "!! The migration check couldn't run (exit $CHECK_RC) - nothing was changed; the previous version is still serving." >&2
  echo "   Look at the lines above: usually DATABASE_URL in $APP_DIR/.env (wrong password / host) or the image." >&2
  exit 1
else
  RDS_ID="$(env_value RDS_INSTANCE_ID)"
  if [[ "$(env_value SKIP_PRE_MIGRATION_SNAPSHOT)" == "1" ]]; then
    echo "!! SKIP_PRE_MIGRATION_SNAPSHOT=1 - migrating without a snapshot" >&2
  elif [[ -z "$RDS_ID" ]]; then
    echo "!! Migrations are pending but RDS_INSTANCE_ID is not set in $APP_DIR/.env, so no snapshot" >&2
    echo "   can be taken first. Set it (or SKIP_PRE_MIGRATION_SNAPSHOT=1 to accept the risk) and redeploy." >&2
    exit 1
  else
    SNAP="pre-migrate-$(date -u +%Y%m%d-%H%M%S)-${IMAGE##*:}"; SNAP="${SNAP:0:63}"
    echo "== Snapshot $RDS_ID -> $SNAP (a few minutes)"
    aws rds create-db-snapshot --region "$REGION" --db-instance-identifier "$RDS_ID"         --db-snapshot-identifier "$SNAP" --tags Key=purpose,Value=pre-migration >/dev/null
    aws rds wait db-snapshot-available --region "$REGION" --db-snapshot-identifier "$SNAP"
    echo "Snapshot ready: $SNAP"
  fi
  echo "== Apply migrations"
  if ! migrate upgrade; then
    echo "!! Migration failed - the app was NOT changed; the previous version is still serving." >&2
    echo "   Each migration runs in its own transaction, so a failed one left nothing half-applied." >&2
    exit 1
  fi
fi

wait_healthy() {
  local deadline=$((SECONDS + WAIT_SECONDS))
  until curl -fsS --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then return 1; fi
    sleep 5
  done
}

echo "== Start $IMAGE"
APP_IMAGE="$IMAGE" $COMPOSE up -d --remove-orphans

if wait_healthy; then
  echo "Healthy: $(curl -fsS "$HEALTH_URL")"
  echo "$IMAGE" > "$APP_DIR/current_image"
  [[ -n "$PREVIOUS" && "$PREVIOUS" != "$IMAGE" ]] && echo "$PREVIOUS" > "$APP_DIR/previous_image"
  # Keep the new image and the one before it (rollback target); everything older goes
  echo "== Clean-up"
  prune_images "$IMAGE" "$PREVIOUS"
  echo "   $(free_gb) GB free"
  exit 0
fi

echo "!! New image not healthy after ${WAIT_SECONDS}s — last log lines:" >&2
docker logs --tail 80 salesai-web >&2 || true
if [[ -n "$PREVIOUS" && "$PREVIOUS" != "$IMAGE" ]]; then
  echo "== Rolling back to $PREVIOUS" >&2
  APP_IMAGE="$PREVIOUS" $COMPOSE up -d --remove-orphans
  if wait_healthy; then echo "Rolled back; previous version is serving." >&2; else echo "Rollback is unhealthy too — check the database and .env." >&2; fi
  echo "   (Migrations applied above stay in place: they are backward-compatible by rule.)" >&2
fi
exit 1
