#!/usr/bin/env bash
# Deploy reviewed Lambda sources with local rollback packages and waiters.
set -euo pipefail
PATH=/usr/local/bin:/usr/bin:/bin
export PATH

RELEASE_SHA="${1:-}"
if [[ ! "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "usage: $0 <full-40-character-release-sha>" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d /tmp/serv-lambda-deploy.XXXXXX)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ROLLBACK="$HOME/serv-rollbacks/$STAMP"
mkdir -p "$ROLLBACK"
trap 'rm -rf "$WORK"' EXIT

snapshot_current() {
  local function_name="$1" location
  location="$(aws lambda get-function \
    --function-name "$function_name" \
    --query 'Code.Location' --output text)"
  curl -fsS "$location" -o "$ROLLBACK/$function_name.zip"
  sha256sum "$ROLLBACK/$function_name.zip" \
    > "$ROLLBACK/$function_name.zip.sha256"
  aws lambda publish-version \
    --function-name "$function_name" \
    --description "pre-$RELEASE_SHA rollback $STAMP" \
    --query '{Version:Version,CodeSha256:CodeSha256}' --output json \
    > "$ROLLBACK/$function_name.version.json" 2>/dev/null || true
}

deploy_zip() {
  local function_name="$1" zip_path="$2"
  snapshot_current "$function_name"
  aws lambda update-function-code \
    --function-name "$function_name" \
    --zip-file "fileb://$zip_path" \
    --query '{FunctionName:FunctionName,CodeSha256:CodeSha256}' --output json
  aws lambda wait function-updated --function-name "$function_name"
  aws lambda get-function-configuration \
    --function-name "$function_name" \
    --query '{FunctionName:FunctionName,LastUpdateStatus:LastUpdateStatus,State:State,CodeSha256:CodeSha256}' \
    --output json
}

package_python_with_location_time() {
  local source_dir="$1" output="$2"
  local stage_dir
  stage_dir="$(mktemp -d "$WORK/python-package.XXXXXX")"
  cp "$source_dir/lambda_function.py" "$stage_dir/lambda_function.py"
  cp "$ROOT/aws/lambdas/location_time.py" "$stage_dir/location_time.py"
  (cd "$stage_dir" && zip -q -X "$output" \
    lambda_function.py location_time.py)
}

package_python_with_location_time "$ROOT/aws/lambdas/insert_detection_event" \
  "$WORK/InsertDetectionEvent.zip"
package_python_with_location_time "$ROOT/aws/lambdas/aggregate_detection_events" \
  "$WORK/AggregateDetectionEvents.zip"
package_python_with_location_time "$ROOT/aws/lambdas/analytics" \
  "$WORK/Analytics.zip"
(cd "$ROOT/aws/lambdas/insert_data" && \
  zip -q -X "$WORK/InsertData.zip" index.mjs visit_schema.mjs)

# Heartbeat was not originally in Git. Patch the exact current live package
# after saving the untouched zip; fail closed if its reviewed anchor changed.
snapshot_current Heartbeat
mkdir -p "$WORK/heartbeat"
unzip -q "$ROLLBACK/Heartbeat.zip" -d "$WORK/heartbeat"
python3 "$ROOT/aws/patch_heartbeat.py" "$WORK/heartbeat/lambda_function.py"
(cd "$WORK/heartbeat" && zip -q -X "$WORK/Heartbeat.zip" lambda_function.py)

deploy_zip InsertDetectionEvent "$WORK/InsertDetectionEvent.zip"
deploy_zip AggregateDetectionEvents "$WORK/AggregateDetectionEvents.zip"
deploy_zip Analytics "$WORK/Analytics.zip"
deploy_zip InsertData "$WORK/InsertData.zip"

# Heartbeat snapshot was already taken above, so update it directly.
aws lambda update-function-code \
  --function-name Heartbeat \
  --zip-file "fileb://$WORK/Heartbeat.zip" \
  --query '{FunctionName:FunctionName,CodeSha256:CodeSha256}' --output json
aws lambda wait function-updated --function-name Heartbeat
aws lambda get-function-configuration \
  --function-name Heartbeat \
  --query '{FunctionName:FunctionName,LastUpdateStatus:LastUpdateStatus,State:State,CodeSha256:CodeSha256}' \
  --output json

printf '%s\n' "$RELEASE_SHA" > "$ROLLBACK/release_sha"
echo "rollback packages: $ROLLBACK"
