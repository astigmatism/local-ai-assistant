#!/usr/bin/env bash
set -Eeuo pipefail

# Run from a temporary copy so a git pull can safely update this script while it is running.
if [[ "${UPDATE_RESTART_SELF_WRAPPED:-0}" != "1" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  TMP_SCRIPT="$(mktemp /tmp/local-ai-assistant-update-and-restart-XXXXXX.sh)"

  cp "${BASH_SOURCE[0]}" "$TMP_SCRIPT"
  chmod +x "$TMP_SCRIPT"

  set +e
  UPDATE_RESTART_SELF_WRAPPED=1 \
  UPDATE_RESTART_PROJECT_ROOT="$SCRIPT_DIR" \
    bash "$TMP_SCRIPT" "$@"
  STATUS=$?
  set -e

  rm -f "$TMP_SCRIPT"
  exit "$STATUS"
fi

SERVICE="${SERVICE:-voice-assistant}"
REMOTE="${REMOTE:-origin}"
BRANCH="${BRANCH:-main}"

TEST_MODE="${TEST_MODE:-container}"
TEST_CMD="${TEST_CMD:-PYTHONPATH=src python -m pytest -q}"

HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8080/api/health}"
HEALTH_RETRIES="${HEALTH_RETRIES:-30}"
HEALTH_SLEEP_SECONDS="${HEALTH_SLEEP_SECONDS:-2}"

usage() {
  cat <<EOF
Usage:
  ./update_and_restart.sh

Environment overrides:
  SERVICE=voice-assistant
  REMOTE=origin
  BRANCH=main
  TEST_MODE=container|host
  TEST_CMD='PYTHONPATH=src python -m pytest -q'
  HEALTH_URL=http://127.0.0.1:8080/api/health
  HEALTH_RETRIES=30
  HEALTH_SLEEP_SECONDS=2

Behavior:
  1. Refuses to run if tracked files have local changes.
  2. Pulls latest source with git pull --ff-only.
  3. Builds the Docker Compose service.
  4. Runs tests.
  5. Recreates/restarts the app only after tests pass.
  6. Waits for the app health endpoint.
EOF
}

log() {
  printf '\n== %s ==\n' "$*"
}

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

ROOT_DIR="$(cd "${UPDATE_RESTART_PROJECT_ROOT:-$(dirname "${BASH_SOURCE[0]}")}" && pwd)"
cd "$ROOT_DIR"

LOCK_FILE="$ROOT_DIR/.update_and_restart.lock"
exec 9>"$LOCK_FILE"

if ! flock -n 9; then
  fail "another update_and_restart.sh run is already active"
fi

log "Deployment start"
date -Is
echo "root: $ROOT_DIR"
echo "service: $SERVICE"
echo "branch: $BRANCH"
echo "test mode: $TEST_MODE"
echo "test command: $TEST_CMD"

log "Verify required commands"
command -v git >/dev/null || fail "git is not installed"
command -v docker >/dev/null || fail "docker is not installed"
command -v curl >/dev/null || fail "curl is not installed"
command -v flock >/dev/null || fail "flock is not installed"
docker compose version >/dev/null || fail "docker compose is not available"

log "Verify tracked working tree is clean"
if ! git diff --quiet --; then
  git status --short
  fail "tracked files have unstaged changes; commit, stash, or reset them first"
fi

if ! git diff --cached --quiet --; then
  git status --short
  fail "tracked files have staged changes; commit, stash, or reset them first"
fi

echo "tracked working tree clean"
echo "untracked files are allowed"

log "Current revision and running container"
OLD_COMMIT="$(git rev-parse HEAD)"
echo "current commit: $OLD_COMMIT"

if docker inspect local-voice-assistant >/dev/null 2>&1; then
  OLD_IMAGE="$(docker inspect local-voice-assistant --format '{{.Image}}' 2>/dev/null || true)"
  echo "current container image: ${OLD_IMAGE:-unknown}"
else
  echo "current container image: none"
fi

docker compose ps || true

log "Fetch and pull latest source"
git fetch --prune "$REMOTE"

CURRENT_BRANCH="$(git branch --show-current)"
if [[ "$CURRENT_BRANCH" != "$BRANCH" ]]; then
  fail "current branch is '$CURRENT_BRANCH', expected '$BRANCH'"
fi

git pull --ff-only "$REMOTE" "$BRANCH"

NEW_COMMIT="$(git rev-parse HEAD)"
echo "new commit: $NEW_COMMIT"

if [[ "$OLD_COMMIT" == "$NEW_COMMIT" ]]; then
  echo "source already up to date"
fi

log "Build updated image"
docker compose build "$SERVICE"

log "Run tests"
case "$TEST_MODE" in
  container)
    docker compose run --rm --no-deps --entrypoint sh "$SERVICE" -lc "$TEST_CMD"
    ;;
  host)
    bash -lc "$TEST_CMD"
    ;;
  *)
    fail "unsupported TEST_MODE: $TEST_MODE"
    ;;
esac

log "Restart application"
docker compose up -d --force-recreate "$SERVICE"

log "Wait for health"
attempt=1
while [[ "$attempt" -le "$HEALTH_RETRIES" ]]; do
  if curl -fsS --max-time 5 "$HEALTH_URL" >/tmp/local-ai-assistant-health.json; then
    echo "healthy on attempt $attempt"
    cat /tmp/local-ai-assistant-health.json
    echo
    rm -f /tmp/local-ai-assistant-health.json
    break
  fi

  echo "health check failed on attempt $attempt/$HEALTH_RETRIES"
  sleep "$HEALTH_SLEEP_SECONDS"
  attempt=$((attempt + 1))
done

if [[ "$attempt" -gt "$HEALTH_RETRIES" ]]; then
  log "Health failed; recent logs"
  docker compose ps || true
  docker logs --tail 160 local-voice-assistant 2>&1 || true
  fail "application did not become healthy after restart"
fi

log "Final status"
docker compose ps
curl -sS --max-time 10 http://127.0.0.1:8080/api/status || true
echo

log "Deployment complete"
date -Is