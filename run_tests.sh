#!/usr/bin/env bash
#
# Build required test images, then run the pytest suite inside a disposable
# "mgmt" container.  Produces a JUnit XML report and per-test failure artifacts
# (FRR configs, route tables, hash state, logs, sink statistics).
#
set -euo pipefail

# -- verify Docker is available ----------------------------------------------
if ! docker info >/dev/null 2>&1; then
    echo "[FAIL]  Docker is not running or not installed.  Please start Docker and retry." >&2
    exit 1
fi

# -- verify kernel supports multipath hash fields -----------------------------
HASH_FIELDS="/proc/sys/net/ipv4/fib_multipath_hash_fields"
if [ ! -f "$HASH_FIELDS" ]; then
    echo "[FAIL]  Kernel does not expose $HASH_FIELDS (need >= 4.13)." >&2
    exit 1
fi

PREFIX="stepanenko"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGES_DIR="$ROOT/images"
MGMT_IMAGE="$PREFIX/mgmt_container"
MGMT_NAME="mgmt"

# -- output directories (one per run, never overwritten) -------------------

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
REPORTS_HOST="$ROOT/reports/$TIMESTAMP"
ALLURE_HOST="$ROOT/allure-results/$TIMESTAMP"
ARTIFACTS_HOST="$ROOT/artifacts/$TIMESTAMP"
mkdir -p "$REPORTS_HOST" "$ALLURE_HOST" "$ARTIFACTS_HOST"

# -- pull external images ---------------------------------------------------

EXTERNAL_IMAGES=(
    "networkop/cx:5.3.0"
)

for ext_image in "${EXTERNAL_IMAGES[@]}"; do
    if docker image inspect "$ext_image" >/dev/null 2>&1; then
        echo "[OK]    external image already present: $ext_image"
    else
        echo "[PULL]  pulling external image: $ext_image"
        docker pull "$ext_image"
    fi
done

# -- build missing images ---------------------------------------------------

for dockerfile_dir in "$IMAGES_DIR"/*/; do
    name="$(basename "$dockerfile_dir")"
    image="$PREFIX/$name"

    if docker image inspect "$image" >/dev/null 2>&1; then
        echo "[OK]    image already present: $image"
    else
        echo "[BUILD] missing image, building: $image"
        docker build -t "$image" -f "$dockerfile_dir/Dockerfile" "$ROOT"
    fi
done

# -- always remove the mgmt container on exit --------------------------------

cleanup() {
    docker rm -f "$MGMT_NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup

# -- run pytest inside the mgmt container ------------------------------------

echo "[RUN]   starting test suite"

set +e
docker run \
    --name "$MGMT_NAME" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$ROOT/tests":"$ROOT/tests" \
    -v "$ROOT/libs":"$ROOT/libs" \
    -v "$REPORTS_HOST":"$ROOT/reports" \
    -v "$ALLURE_HOST":"$ROOT/allure-results" \
    -v "$ARTIFACTS_HOST":"$ROOT/artifacts" \
    -e "ARTIFACTS_DIR=$ROOT/artifacts" \
    -w "$ROOT/tests" \
    "$MGMT_IMAGE" \
    pytest \
      --junitxml="$ROOT/reports/junit.xml" \
      --alluredir="$ROOT/allure-results" \
      -o junit_family=xunit2
rc=$?
set -e

# -- summary ------------------------------------------------------------------

echo ""
echo "========================================"
echo "  ECMP Test Suite  —  Results"
echo "========================================"
echo "  Exit code      : $rc"
echo "  JUnit report   : $REPORTS_HOST/junit.xml"
echo "  Allure results  : $ALLURE_HOST"
echo "  Artifacts      : $ARTIFACTS_HOST"
echo ""
echo "  View Allure report (requires 'allure' CLI):"
echo "    allure serve $ALLURE_HOST"
echo "  Or upload to Allure TestOps / ReportPortal."
echo "========================================"

exit "$rc"
