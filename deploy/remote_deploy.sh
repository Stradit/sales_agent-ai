#!/usr/bin/env bash
# Runs ON the EC2 host (as "deploy"), called by GitHub Actions over SSH:
#
#   bash /opt/salesai/remote_deploy.sh <ecr-image-uri>
#
# Pulls the image, starts it, waits for /api/health and rolls back to the previous image if
# the new one doesn't become healthy. The schema is created/upgraded by the app on startup
# (db/create_tables.ensure_schema_compatibility + the CRM/deals/copilot ensure_schema calls).
set -euo pipefail

IMAGE="${1:?usage: remote_deploy.sh <ecr-image-uri>}"
APP_DIR=/opt/salesai
COMPOSE="docker compose -f $APP_DIR/docker-compose.prod.yml"
REGION="${AWS_REGION:-us-east-1}"
REGISTRY="${IMAGE%%/*}"
HEALTH_URL=http://127.0.0.1:8000/api/health
WAIT_SECONDS=180

[[ -f "$APP_DIR/.env" ]] || { echo "Missing $APP_DIR/.env" >&2; exit 1; }

echo "== ECR login ($REGISTRY)"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY"

echo "== Pull $IMAGE"
docker pull "$IMAGE"

PREVIOUS="$(docker inspect --format '{{.Config.Image}}' salesai-web 2>/dev/null || true)"
echo "Previous image: ${PREVIOUS:-none}"

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
  # Keep the running and previous images; drop older ones
  docker image prune -af --filter "until=168h" >/dev/null || true
  exit 0
fi

echo "!! New image not healthy after ${WAIT_SECONDS}s — last log lines:" >&2
docker logs --tail 80 salesai-web >&2 || true
if [[ -n "$PREVIOUS" && "$PREVIOUS" != "$IMAGE" ]]; then
  echo "== Rolling back to $PREVIOUS" >&2
  APP_IMAGE="$PREVIOUS" $COMPOSE up -d --remove-orphans
  if wait_healthy; then echo "Rolled back; previous version is serving." >&2; else echo "Rollback is unhealthy too — check the database and .env." >&2; fi
fi
exit 1
