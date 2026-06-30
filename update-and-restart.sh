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

TEST_MODE="${TEST_MODE:-container-mounted}"
TEST_CMD="${TEST_CMD:-PYTHONPATH=src python -m pytest -q}"
TEST_PATHS="${TEST_PATHS:-tests}"
REQUIRE_TESTS="${REQUIRE_TESTS:-true}"
CONTAINER_TEST_PACKAGES="${CONTAINER_TEST_PACKAGES:-pytest pytest-asyncio}"
CONTAINER_TEST_WORKDIR="${CONTAINER_TEST_WORKDIR:-/workspace}"
CONTAINER_TEST_VENV="${CONTAINER_TEST_VENV:-/tmp/local-ai-assistant-test-venv}"
TEST_ENV_UNSET="${TEST_ENV_UNSET:-VOICE_ASSISTANT_CONFIG TTS_ROUTER_API_KEY WHISPER_API_KEY VOICE_ASSISTANT_POCKETSPHINX_CUSTOM_DICT VOICE_ASSISTANT_POCKETSPHINX_THRESHOLD VOICE_ASSISTANT_WAKE_COOLDOWN_SECONDS VOICE_ASSISTANT_WAKE_HOP_SECONDS VOICE_ASSISTANT_WAKE_WINDOW_SECONDS}"

BUILD_NO_CACHE="${BUILD_NO_CACHE:-true}"

VERIFY_BUILT_IMAGE_SOURCE="${VERIFY_BUILT_IMAGE_SOURCE:-true}"
VERIFY_RUNNING_SOURCE="${VERIFY_RUNNING_SOURCE:-true}"
CONTAINER_SOURCE_ROOT="${CONTAINER_SOURCE_ROOT:-/app/src}"
PYTHON_PACKAGE_NAME="${PYTHON_PACKAGE_NAME:-voice_assistant}"

HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8080/api/health}"
STATUS_URL="${STATUS_URL:-http://127.0.0.1:8080/api/status}"
HEALTH_RETRIES="${HEALTH_RETRIES:-30}"
HEALTH_SLEEP_SECONDS="${HEALTH_SLEEP_SECONDS:-2}"

LOCK_FILE="${LOCK_FILE:-/tmp/local-ai-assistant-update-and-restart.lock}"

APP_RESTART_STARTED=0
APP_RESTART_COMPLETED=0
VERIFIER_FILE=""
HEALTH_RESPONSE_FILE=""

usage() {
  cat <<EOF
Usage:
  ./update-and-restart.sh

Environment overrides:
  SERVICE=voice-assistant
  CONTAINER_NAME=local-voice-assistant
  REMOTE=origin
  BRANCH=main

  TEST_MODE=container-mounted|host|none
  TEST_CMD='PYTHONPATH=src python -m pytest -q'
  TEST_PATHS=tests
  REQUIRE_TESTS=true|false
  CONTAINER_TEST_PACKAGES='pytest pytest-asyncio'
  CONTAINER_TEST_WORKDIR=/workspace
  CONTAINER_TEST_VENV=/tmp/local-ai-assistant-test-venv
  TEST_ENV_UNSET='VOICE_ASSISTANT_CONFIG TTS_ROUTER_API_KEY WHISPER_API_KEY VOICE_ASSISTANT_POCKETSPHINX_CUSTOM_DICT VOICE_ASSISTANT_POCKETSPHINX_THRESHOLD VOICE_ASSISTANT_WAKE_COOLDOWN_SECONDS VOICE_ASSISTANT_WAKE_HOP_SECONDS VOICE_ASSISTANT_WAKE_WINDOW_SECONDS'

  BUILD_NO_CACHE=true|false

  VERIFY_BUILT_IMAGE_SOURCE=true|false
  VERIFY_RUNNING_SOURCE=true|false
  CONTAINER_SOURCE_ROOT=/app/src
  PYTHON_PACKAGE_NAME=voice_assistant

  HEALTH_URL=http://127.0.0.1:8080/api/health
  STATUS_URL=http://127.0.0.1:8080/api/status
  HEALTH_RETRIES=30
  HEALTH_SLEEP_SECONDS=2

  LOCK_FILE=/tmp/local-ai-assistant-update-and-restart.lock

Behavior:
  1. Refuses to run if tracked files have local changes.
  2. Pulls latest source with git pull --ff-only.
  3. Calculates a hash of the pulled Python source tree.
  4. Builds the Docker Compose service with Docker cache disabled by default.
  5. Verifies the freshly built image contains the pulled /app/src source and matching installed package.
  6. Runs tests before restart.
     - Default mode is container-mounted.
     - The production checkout is mounted read-only into a one-off test container.
     - A temporary test venv is created inside that one-off container.
     - pytest and pytest-asyncio are installed into that temporary venv.
     - Production runtime env vars are unset before tests so tests use their expected defaults.
     - The runtime image and host checkout are not modified by test setup.
  7. Recreates/restarts the app only after tests pass.
  8. Waits for the app health endpoint.
  9. Verifies the running container's /app/src source matches the pulled source.
  10. Verifies the imported runtime Python package matches /app/src so stale site-packages installs cannot be served.
EOF
}

log() {
  printf '\n== %s ==\n' "$*"
}

print_failure_state() {
  if [[ "$APP_RESTART_STARTED" != "1" ]]; then
    echo "Deployment stopped before application restart. The running application was not changed by this run." >&2
  elif [[ "$APP_RESTART_COMPLETED" != "1" ]]; then
    echo "Application restart was attempted but did not complete cleanly. Check docker compose ps and container logs." >&2
  else
    echo "Application restart completed, but a post-restart verification failed. Check docker compose ps and container logs." >&2
  fi
}

fail() {
  echo "ERROR: $*" >&2
  print_failure_state
  exit 1
}

on_error() {
  local status=$?
  echo "ERROR: deployment failed with exit status $status" >&2
  print_failure_state
  exit "$status"
}

cleanup() {
  if [[ -n "$VERIFIER_FILE" ]]; then
    rm -f "$VERIFIER_FILE"
  fi

  if [[ -n "$HEALTH_RESPONSE_FILE" ]]; then
    rm -f "$HEALTH_RESPONSE_FILE"
  fi
}

trap on_error ERR
trap cleanup EXIT

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

write_source_verifier() {
  VERIFIER_FILE="$(mktemp /tmp/local-ai-assistant-source-verify-XXXXXX.py)"

  cat > "$VERIFIER_FILE" <<'PY'
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

verify_built_service_image_source_consistency() {
  local expected_source_hash="$1"

  if ! is_true "$VERIFY_BUILT_IMAGE_SOURCE"; then
    echo "built image source verification disabled"
    return 0
  fi

  [[ -n "$VERIFIER_FILE" ]] || fail "source verifier file has not been created"

  docker compose run \
    --rm \
    --no-deps \
    -T \
    --entrypoint python \
    -e EXPECTED_SOURCE_HASH="$expected_source_hash" \
    -e CONTAINER_SOURCE_ROOT="$CONTAINER_SOURCE_ROOT" \
    -e PYTHON_PACKAGE_NAME="$PYTHON_PACKAGE_NAME" \
    "$SERVICE" - < "$VERIFIER_FILE"
}

verify_running_container_source_consistency() {
  local expected_source_hash="$1"

  if ! is_true "$VERIFY_RUNNING_SOURCE"; then
    echo "running source verification disabled"
    return 0
  fi

  [[ -n "$VERIFIER_FILE" ]] || fail "source verifier file has not been created"

  docker exec -i \
    -e EXPECTED_SOURCE_HASH="$expected_source_hash" \
    -e CONTAINER_SOURCE_ROOT="$CONTAINER_SOURCE_ROOT" \
    -e PYTHON_PACKAGE_NAME="$PYTHON_PACKAGE_NAME" \
    "$CONTAINER_NAME" \
    python - < "$VERIFIER_FILE"
}

run_host_tests() {
  bash -lc "$TEST_CMD"
}

run_container_mounted_tests() {
  local test_setup_script

  test_setup_script="$(cat <<'SH'
set -eu

echo "test workdir: $(pwd)"
echo "test command: ${TEST_CMD}"
echo "test paths: ${TEST_PATHS}"
echo "test packages: ${CONTAINER_TEST_PACKAGES}"
echo "test venv: ${CONTAINER_TEST_VENV}"
echo "test env unset: ${TEST_ENV_UNSET}"

for var_name in ${TEST_ENV_UNSET}; do
  unset "$var_name"
done

if [ "${REQUIRE_TESTS}" = "true" ]; then
  missing_paths=0
  for path in ${TEST_PATHS}; do
    if [ ! -e "$path" ]; then
      echo "ERROR: required test path is missing in mounted checkout: $path" >&2
      missing_paths=1
    fi
  done

  if [ "$missing_paths" != "0" ]; then
    exit 1
  fi
fi

rm -rf "${CONTAINER_TEST_VENV}"
python -m venv --system-site-packages "${CONTAINER_TEST_VENV}"

"${CONTAINER_TEST_VENV}/bin/python" -m pip install --no-cache-dir ${CONTAINER_TEST_PACKAGES}
"${CONTAINER_TEST_VENV}/bin/python" -m pytest --version

export PATH="${CONTAINER_TEST_VENV}/bin:${PATH}"
export PYTHONPYCACHEPREFIX=/tmp/local-ai-assistant-pycache
export PYTEST_ADDOPTS="${PYTEST_ADDOPTS:-} -p no:cacheprovider"

echo "python used for tests: $(command -v python)"
python - <<'PY'
import os
import pytest
import sys

print("pytest module:", pytest.__file__)
print("python executable:", sys.executable)
print("VOICE_ASSISTANT_WAKE_WINDOW_SECONDS:", os.environ.get("VOICE_ASSISTANT_WAKE_WINDOW_SECONDS"))
PY

sh -c "$TEST_CMD"
SH
)"

  docker compose run \
    --rm \
    --no-deps \
    -T \
    --entrypoint sh \
    --workdir "$CONTAINER_TEST_WORKDIR" \
    --volume "$ROOT_DIR:$CONTAINER_TEST_WORKDIR:ro" \
    -e TEST_CMD="$TEST_CMD" \
    -e TEST_PATHS="$TEST_PATHS" \
    -e REQUIRE_TESTS="$REQUIRE_TESTS" \
    -e CONTAINER_TEST_PACKAGES="$CONTAINER_TEST_PACKAGES" \
    -e CONTAINER_TEST_VENV="$CONTAINER_TEST_VENV" \
    -e TEST_ENV_UNSET="$TEST_ENV_UNSET" \
    "$SERVICE" \
    -c "$test_setup_script"
}

run_tests() {
  case "$TEST_MODE" in
    container-mounted)
      run_container_mounted_tests
      ;;
    host)
      run_host_tests
      ;;
    none)
      echo "test execution disabled by TEST_MODE=none"
      ;;
    *)
      fail "unsupported TEST_MODE: $TEST_MODE"
      ;;
  esac
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

ROOT_DIR="$(cd "${UPDATE_RESTART_PROJECT_ROOT:-$(dirname "${BASH_SOURCE[0]}")}" && pwd)"
cd "$ROOT_DIR"

write_source_verifier

exec 9>"$LOCK_FILE"

if ! flock -n 9; then
  fail "another update-and-restart run is already active"
fi

log "Deployment start"
date -Is
echo "root: $ROOT_DIR"
echo "service: $SERVICE"
echo "container: $CONTAINER_NAME"
echo "branch: $BRANCH"
echo "test mode: $TEST_MODE"
echo "test command: $TEST_CMD"
echo "test paths: $TEST_PATHS"
echo "require tests: $REQUIRE_TESTS"
echo "container test packages: $CONTAINER_TEST_PACKAGES"
echo "test env unset: $TEST_ENV_UNSET"
echo "build no cache: $BUILD_NO_CACHE"
echo "verify built image source: $VERIFY_BUILT_IMAGE_SOURCE"
echo "verify running source: $VERIFY_RUNNING_SOURCE"
echo "lock file: $LOCK_FILE"

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

log "Verify built image source and installed package"
verify_built_service_image_source_consistency "$EXPECTED_SOURCE_HASH"

log "Run tests"
run_tests

log "Restart application"
APP_RESTART_STARTED=1
docker compose up -d --force-recreate "$SERVICE"
APP_RESTART_COMPLETED=1

log "Wait for health"
HEALTH_RESPONSE_FILE="$(mktemp /tmp/local-ai-assistant-health-XXXXXX.json)"

attempt=1
while [[ "$attempt" -le "$HEALTH_RETRIES" ]]; do
  if curl -fsS --max-time 5 "$HEALTH_URL" > "$HEALTH_RESPONSE_FILE"; then
    echo "healthy on attempt $attempt"
    cat "$HEALTH_RESPONSE_FILE"
    echo
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
curl -sS --max-time 10 "$STATUS_URL" || true
echo

log "Deployment complete"
date -Is