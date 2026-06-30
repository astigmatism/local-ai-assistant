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
CONTAINER_NAME="${CONTAINER_NAME:-local-voice-assistant}"
REMOTE="${REMOTE:-origin}"
BRANCH="${BRANCH:-main}"

TEST_MODE="${TEST_MODE:-container}"
TEST_CMD="${TEST_CMD:-PYTHONPATH=src python -m pytest -q}"

BUILD_NO_CACHE="${BUILD_NO_CACHE:-true}"

VERIFY_RUNNING_SOURCE="${VERIFY_RUNNING_SOURCE:-true}"
CONTAINER_SOURCE_ROOT="${CONTAINER_SOURCE_ROOT:-/app/src}"
PYTHON_PACKAGE_NAME="${PYTHON_PACKAGE_NAME:-voice_assistant}"

HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8080/api/health}"
HEALTH_RETRIES="${HEALTH_RETRIES:-30}"
HEALTH_SLEEP_SECONDS="${HEALTH_SLEEP_SECONDS:-2}"

usage() {
  cat <<EOF
Usage:
  ./update_and_restart.sh

Environment overrides:
  SERVICE=voice-assistant
  CONTAINER_NAME=local-voice-assistant
  REMOTE=origin
  BRANCH=main
  TEST_MODE=container|host
  TEST_CMD='PYTHONPATH=src python -m pytest -q'
  BUILD_NO_CACHE=true|false
  VERIFY_RUNNING_SOURCE=true|false
  CONTAINER_SOURCE_ROOT=/app/src
  PYTHON_PACKAGE_NAME=voice_assistant
  HEALTH_URL=http://127.0.0.1:8080/api/health
  HEALTH_RETRIES=30
  HEALTH_SLEEP_SECONDS=2

Behavior:
  1. Refuses to run if tracked files have local changes.
  2. Pulls latest source with git pull --ff-only.
  3. Builds the Docker Compose service with Docker cache disabled by default.
  4. Runs tests.
  5. Recreates/restarts the app only after tests pass.
  6. Waits for the app health endpoint.
  7. Verifies the running container uses the freshly built image.
  8. Verifies the running container's /app/src Python source matches the pulled source.
  9. Verifies the imported runtime Python package matches /app/src so stale site-packages installs cannot be served.
EOF
}

log() {
  printf '\n== %s ==\n' "$*"
}

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

is_true() {
  case "${1,,}" in
    1|true|yes|y|on)
      return 0
      ;;
    0|false|no|n|off)
      return 1
      ;;
    *)
      fail "invalid boolean value: $1"
      ;;
  esac
}

hash_python_source_tree() {
  local source_root="$1"

  [[ -d "$source_root" ]] || fail "source directory does not exist: $source_root"

  (
    cd "$source_root"
    find . -type f -name '*.py' ! -path '*/__pycache__/*' \
      | LC_ALL=C sort \
      | while IFS= read -r file; do
          sha256sum "$file"
        done \
      | sha256sum \
      | awk '{print $1}'
  )
}

verify_running_container_source_consistency() {
  local expected_source_hash="$1"

  if ! is_true "$VERIFY_RUNNING_SOURCE"; then
    echo "running source verification disabled"
    return 0
  fi

  docker exec -i \
    -e EXPECTED_SOURCE_HASH="$expected_source_hash" \
    -e CONTAINER_SOURCE_ROOT="$CONTAINER_SOURCE_ROOT" \
    -e PYTHON_PACKAGE_NAME="$PYTHON_PACKAGE_NAME" \
    "$CONTAINER_NAME" \
    python - <<'PY'
import hashlib
import importlib
import os
import sys
from pathlib import Path


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


expected_source_hash = os.environ["EXPECTED_SOURCE_HASH"]
source_root = Path(os.environ.get("CONTAINER_SOURCE_ROOT", "/app/src")).resolve()
package_name = os.environ.get("PYTHON_PACKAGE_NAME", "voice_assistant")


def tree_hash(root: Path) -> tuple[str, int]:
    if not root.exists():
        fail(f"container source root does not exist: {root}")

    digest = hashlib.sha256()
    file_count = 0

    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue

        relative_path = path.relative_to(root).as_posix()
        file_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        digest.update(f"{file_digest}  ./{relative_path}\n".encode("utf-8"))
        file_count += 1

    return digest.hexdigest(), file_count


actual_source_hash, source_file_count = tree_hash(source_root)

if actual_source_hash != expected_source_hash:
    fail(
        "container /app/src Python source does not match the pulled host source "
        f"(expected {expected_source_hash}, got {actual_source_hash})"
    )

try:
    package = importlib.import_module(package_name)
except Exception as exc:
    fail(f"could not import runtime package {package_name!r}: {exc}")

if not getattr(package, "__file__", None):
    fail(f"runtime package {package_name!r} has no __file__")

runtime_package_root = Path(package.__file__).resolve().parent
source_package_root = source_root / package_name

if not source_package_root.exists():
    fail(f"source package directory does not exist: {source_package_root}")

mismatches: list[str] = []

for source_file in sorted(source_package_root.rglob("*.py")):
    if "__pycache__" in source_file.parts:
        continue

    relative_path = source_file.relative_to(source_package_root)
    runtime_file = runtime_package_root / relative_path

    if not runtime_file.exists():
        mismatches.append(f"missing runtime file: {relative_path.as_posix()}")
        continue

    source_digest = hashlib.sha256(source_file.read_bytes()).hexdigest()
    runtime_digest = hashlib.sha256(runtime_file.read_bytes()).hexdigest()

    if source_digest != runtime_digest:
        mismatches.append(f"stale runtime file: {relative_path.as_posix()}")

if mismatches:
    print(
        f"ERROR: imported runtime package at {runtime_package_root} does not match {source_package_root}",
        file=sys.stderr,
    )
    for mismatch in mismatches[:50]:
        print(f"  {mismatch}", file=sys.stderr)
    if len(mismatches) > 50:
        print(f"  ... {len(mismatches) - 50} additional mismatches omitted", file=sys.stderr)
    raise SystemExit(1)

print(f"source_hash: {actual_source_hash}")
print(f"source_files: {source_file_count}")
print(f"source_root: {source_root}")
print(f"runtime_package_root: {runtime_package_root}")
PY
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
echo "container: $CONTAINER_NAME"
echo "branch: $BRANCH"
echo "test mode: $TEST_MODE"
echo "test command: $TEST_CMD"
echo "build no cache: $BUILD_NO_CACHE"
echo "verify running source: $VERIFY_RUNNING_SOURCE"

log "Verify required commands"
command -v git >/dev/null || fail "git is not installed"
command -v docker >/dev/null || fail "docker is not installed"
command -v curl >/dev/null || fail "curl is not installed"
command -v flock >/dev/null || fail "flock is not installed"
command -v sha256sum >/dev/null || fail "sha256sum is not installed"
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

if docker inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
  OLD_IMAGE="$(docker inspect "$CONTAINER_NAME" --format '{{.Image}}' 2>/dev/null || true)"
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

EXPECTED_SOURCE_HASH="$(hash_python_source_tree "$ROOT_DIR/src")"
echo "expected Python source hash: $EXPECTED_SOURCE_HASH"

log "Build updated image"
BUILD_ARGS=()

if is_true "$BUILD_NO_CACHE"; then
  BUILD_ARGS+=(--no-cache)
fi

if [[ "${#BUILD_ARGS[@]}" -gt 0 ]]; then
  echo "docker compose build flags: ${BUILD_ARGS[*]}"
else
  echo "docker compose build flags: none"
fi

docker compose build "${BUILD_ARGS[@]}" "$SERVICE"

BUILT_IMAGE_ID="$(docker compose images -q "$SERVICE" 2>/dev/null | head -n 1 || true)"
echo "built service image: ${BUILT_IMAGE_ID:-unknown}"

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

log "Verify running container image"
RUNNING_IMAGE="$(docker inspect "$CONTAINER_NAME" --format '{{.Image}}' 2>/dev/null || true)"
echo "running container image: ${RUNNING_IMAGE:-unknown}"

if [[ -n "$BUILT_IMAGE_ID" && -n "$RUNNING_IMAGE" ]]; then
  BUILT_IMAGE_NORMALIZED="${BUILT_IMAGE_ID#sha256:}"
  RUNNING_IMAGE_NORMALIZED="${RUNNING_IMAGE#sha256:}"

  if [[ "$BUILT_IMAGE_NORMALIZED" != "$RUNNING_IMAGE_NORMALIZED" ]]; then
    fail "running container image does not match the image built by this deployment"
  fi
fi

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
  docker logs --tail 160 "$CONTAINER_NAME" 2>&1 || true
  fail "application did not become healthy after restart"
fi

log "Verify running source and installed package"
verify_running_container_source_consistency "$EXPECTED_SOURCE_HASH"

log "Final status"
docker compose ps
curl -sS --max-time 10 http://127.0.0.1:8080/api/status || true
echo

log "Deployment complete"
date -Is