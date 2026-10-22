#!/usr/bin/env bash
# Deploy only the two additive Lambdas changed by the vehicle-profile release.
#
# Safety properties:
#   * requires an explicit full release SHA and --deploy;
#   * packages source from that immutable Git object, never the working tree;
#   * refuses to run unless that release is the exact checked-out HEAD and the
#     executing script/source mirrors still match it;
#   * snapshots and checksums all live packages before any update;
#   * waits for and verifies each exact code package before continuing;
#   * never touches any other Lambda function.
set -Eeuo pipefail
umask 077

readonly AWS_DEPLOY_REGION="us-east-1"
readonly EXPECTED_AWS_ACCOUNT_ID="502954288533"

usage() {
  echo "usage: $0 <full-40-character-release-sha> --deploy" >&2
}

die() {
  echo "[vehicle-profile-deploy][FATAL] $*" >&2
  exit 1
}

RELEASE_SHA="${1:-}"
EXECUTION_FLAG="${2:-}"
if [[ ! "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  usage
  die "release SHA must be 40 lowercase hexadecimal characters"
fi
if [[ "$EXECUTION_FLAG" != "--deploy" || $# -ne 2 ]]; then
  usage
  die "the explicit --deploy flag is required"
fi

for required_command in aws curl git openssl tar unzip zip; do
  command -v "$required_command" >/dev/null 2>&1 \
    || die "missing required command: $required_command"
done
if ! command -v sha256sum >/dev/null 2>&1 \
    && ! command -v shasum >/dev/null 2>&1; then
  die "missing SHA-256 utility (need sha256sum or shasum)"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
HEAD_SHA="$(git -C "$REPO_ROOT" rev-parse HEAD)"
[[ "$HEAD_SHA" == "$RELEASE_SHA" ]] \
  || die "release SHA is not exact HEAD (HEAD=$HEAD_SHA)"
git -C "$REPO_ROOT" cat-file -e "${RELEASE_SHA}^{commit}" 2>/dev/null \
  || die "release commit is not present locally"

RELEASE_PATHS=(
  aws/deploy_vehicle_profile_release.sh
  aws/lambdas/insert_data/index.mjs
  aws/lambdas/insert_data/visit_schema.mjs
  aws/lambdas/get_customers/lambda_function.py
)
for release_path in "${RELEASE_PATHS[@]}"; do
  git -C "$REPO_ROOT" cat-file -e \
    "${RELEASE_SHA}:${release_path}" 2>/dev/null \
    || die "release does not contain ${release_path}"
done
if ! git -C "$REPO_ROOT" diff --quiet "$RELEASE_SHA" -- \
    "${RELEASE_PATHS[@]}"; then
  die "deployment script or Lambda sources differ from the release commit"
fi

# Function names are not globally unique. Fail closed before creating release
# artifacts, downloading live packages, or updating Lambda unless the active
# AWS credentials resolve to the one production account in the pinned region.
if ! CALLER_ACCOUNT_ID="$(aws sts get-caller-identity \
    --region "$AWS_DEPLOY_REGION" \
    --query Account --output text)"; then
  die "could not verify the active AWS account in $AWS_DEPLOY_REGION"
fi
[[ "$CALLER_ACCOUNT_ID" == "$EXPECTED_AWS_ACCOUNT_ID" ]] \
  || die "refusing AWS account $CALLER_ACCOUNT_ID; expected $EXPECTED_AWS_ACCOUNT_ID"
echo "AWS deployment target verified: account $CALLER_ACCOUNT_ID, region $AWS_DEPLOY_REGION"

REPO_PARENT="$(dirname "$REPO_ROOT")"
ROLLBACK_ROOT="${SERV_AWS_ROLLBACK_ROOT:-${REPO_PARENT}/serv-aws-rollbacks}"
UTC_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ROLLBACK_DIR="${ROLLBACK_ROOT}/vehicle-profile-${UTC_STAMP}-${RELEASE_SHA:0:12}"
WORK_DIR="$(mktemp -d /tmp/serv-vehicle-profile-deploy.XXXXXX)"
SNAPSHOTS_READY=0
INSERT_DATA_SNAPSHOT_REVISION=""
GET_CUSTOMERS_SNAPSHOT_REVISION=""

cleanup() {
  rm -rf -- "$WORK_DIR"
}

print_rollback() {
  echo "Rollback packages:"
  echo "  $ROLLBACK_DIR/InsertData.zip"
  echo "  $ROLLBACK_DIR/GetCustomers.zip"
  echo "Rollback commands (reverse dependency order: GetCustomers, InsertData):"
  printf '  aws lambda update-function-code --region %q --function-name GetCustomers --zip-file %q\n' \
    "$AWS_DEPLOY_REGION" "fileb://${ROLLBACK_DIR}/GetCustomers.zip"
  printf '  aws lambda wait function-updated --region %q --function-name GetCustomers\n' \
    "$AWS_DEPLOY_REGION"
  printf '  aws lambda update-function-code --region %q --function-name InsertData --zip-file %q\n' \
    "$AWS_DEPLOY_REGION" "fileb://${ROLLBACK_DIR}/InsertData.zip"
  printf '  aws lambda wait function-updated --region %q --function-name InsertData\n' \
    "$AWS_DEPLOY_REGION"
  echo "Manual rollback intentionally omits --revision-id: the pre-release revisions are stale after deployment, but these package snapshots are durable."
}

on_error() {
  local exit_code="$1" line_number="$2"
  trap - ERR
  echo "[vehicle-profile-deploy][FATAL] failed at line ${line_number}" >&2
  if [[ "$SNAPSHOTS_READY" -eq 1 ]]; then
    print_rollback >&2
  else
    echo "Live package snapshots were not completed; no deployment began." >&2
  fi
  exit "$exit_code"
}

trap cleanup EXIT
trap 'on_error $? $LINENO' ERR

mkdir -p "$ROLLBACK_ROOT"
mkdir "$ROLLBACK_DIR" \
  || die "rollback directory already exists: $ROLLBACK_DIR"
printf '%s\n' "$RELEASE_SHA" > "$ROLLBACK_DIR/release_sha"
printf '%s\n' "$HEAD_SHA" > "$ROLLBACK_DIR/head_sha"

checksum_file() {
  local file_path="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$file_path"
  else
    shasum -a 256 "$file_path"
  fi
}

safe_configuration() {
  local function_name="$1" output_path="$2"
  aws lambda get-function-configuration \
    --region "$AWS_DEPLOY_REGION" \
    --function-name "$function_name" \
    --query '{FunctionName:FunctionName,Runtime:Runtime,Handler:Handler,Role:Role,Timeout:Timeout,MemorySize:MemorySize,State:State,LastUpdateStatus:LastUpdateStatus,CodeSha256:CodeSha256,LastModified:LastModified,RevisionId:RevisionId}' \
    --output json > "$output_path"
}

snapshot_current() {
  local function_name="$1" package_path snapshot_info
  local package_url revision_id function_state update_state
  package_path="$ROLLBACK_DIR/${function_name}.zip"
  # One GetFunction response binds the download URL to the RevisionId used as
  # the later optimistic-lock token. A concurrent code or configuration
  # update after this point makes update-function-code fail closed.
  snapshot_info="$(aws lambda get-function \
    --region "$AWS_DEPLOY_REGION" \
    --function-name "$function_name" \
    --query '[Code.Location,Configuration.RevisionId,Configuration.State,Configuration.LastUpdateStatus]' \
    --output text)"
  IFS=$'\t' read -r \
    package_url revision_id function_state update_state <<< "$snapshot_info"
  [[ "$function_state" == "Active" ]] \
    || die "$function_name state is $function_state, not Active"
  [[ "$update_state" == "Successful" ]] \
    || die "$function_name update status is $update_state, not Successful"
  [[ -n "$revision_id" && "$revision_id" != "None" ]] \
    || die "could not obtain RevisionId for $function_name"
  case "$function_name" in
    InsertData) INSERT_DATA_SNAPSHOT_REVISION="$revision_id" ;;
    GetCustomers) GET_CUSTOMERS_SNAPSHOT_REVISION="$revision_id" ;;
    *) die "snapshot requested for out-of-scope function: $function_name" ;;
  esac
  printf '%s\n' "$revision_id" \
    > "$ROLLBACK_DIR/${function_name}.before-revision-id"
  safe_configuration \
    "$function_name" "$ROLLBACK_DIR/${function_name}.before.json"
  [[ "$package_url" == https://* ]] \
    || die "could not obtain download URL for $function_name"
  curl --fail --location --silent --show-error \
    --retry 3 --connect-timeout 10 --max-time 120 \
    --output "$package_path" "$package_url"
  unzip -tq "$package_path" >/dev/null
  checksum_file "$package_path" > "${package_path}.sha256"
}

# Snapshot every in-scope function before making any external change.
snapshot_current InsertData
snapshot_current GetCustomers
SNAPSHOTS_READY=1
echo "Live packages safely snapshotted before deployment."
print_rollback

# Export only reviewed source from the immutable release object.
mkdir "$WORK_DIR/release"
git -C "$REPO_ROOT" archive --format=tar "$RELEASE_SHA" -- \
  aws/lambdas/insert_data aws/lambdas/get_customers \
  | tar -x -C "$WORK_DIR/release"

INSERT_SOURCE="$WORK_DIR/release/aws/lambdas/insert_data"
CUSTOMERS_SOURCE="$WORK_DIR/release/aws/lambdas/get_customers"
INSERT_PACKAGE="$WORK_DIR/InsertData.release.zip"
CUSTOMERS_PACKAGE="$WORK_DIR/GetCustomers.release.zip"

(
  cd "$INSERT_SOURCE"
  zip -q -X "$INSERT_PACKAGE" index.mjs visit_schema.mjs
)
(
  cd "$CUSTOMERS_SOURCE"
  zip -q -X "$CUSTOMERS_PACKAGE" lambda_function.py
)
[[ "$(unzip -Z1 "$INSERT_PACKAGE" | LC_ALL=C sort)" == \
  $'index.mjs\nvisit_schema.mjs' ]] \
  || die "InsertData package contains unexpected files"
[[ "$(unzip -Z1 "$CUSTOMERS_PACKAGE")" == "lambda_function.py" ]] \
  || die "GetCustomers package contains unexpected files"
unzip -tq "$INSERT_PACKAGE" >/dev/null
unzip -tq "$CUSTOMERS_PACKAGE" >/dev/null
INSERT_RELEASE_RECORD="$ROLLBACK_DIR/InsertData.release.zip"
CUSTOMERS_RELEASE_RECORD="$ROLLBACK_DIR/GetCustomers.release.zip"
cp "$INSERT_PACKAGE" "$INSERT_RELEASE_RECORD"
cp "$CUSTOMERS_PACKAGE" "$CUSTOMERS_RELEASE_RECORD"
checksum_file "$INSERT_RELEASE_RECORD" \
  > "${INSERT_RELEASE_RECORD}.sha256"
checksum_file "$CUSTOMERS_RELEASE_RECORD" \
  > "${CUSTOMERS_RELEASE_RECORD}.sha256"

expected_lambda_sha() {
  local package_path="$1"
  openssl dgst -sha256 -binary "$package_path" | openssl base64 -A
}

deploy_and_verify() {
  local function_name="$1" package_path="$2" snapshot_revision="$3"
  local expected_sha actual_sha verification
  local function_state update_state
  [[ -n "$snapshot_revision" ]] \
    || die "missing snapshot RevisionId for $function_name"
  expected_sha="$(expected_lambda_sha "$package_path")"
  aws lambda update-function-code \
    --region "$AWS_DEPLOY_REGION" \
    --function-name "$function_name" \
    --zip-file "fileb://${package_path}" \
    --revision-id "$snapshot_revision" \
    --query '{FunctionName:FunctionName,CodeSha256:CodeSha256,LastModified:LastModified}' \
    --output json > "$ROLLBACK_DIR/${function_name}.update.json"
  aws lambda wait function-updated \
    --region "$AWS_DEPLOY_REGION" \
    --function-name "$function_name"
  safe_configuration \
    "$function_name" "$ROLLBACK_DIR/${function_name}.after.json"
  verification="$(aws lambda get-function-configuration \
    --region "$AWS_DEPLOY_REGION" \
    --function-name "$function_name" \
    --query '[State,LastUpdateStatus,CodeSha256]' --output text)"
  IFS=$'\t' read -r function_state update_state actual_sha <<< "$verification"
  [[ "$function_state" == "Active" ]] \
    || die "$function_name state is $function_state after deployment"
  [[ "$update_state" == "Successful" ]] \
    || die "$function_name update status is $update_state after deployment"
  [[ "$actual_sha" == "$expected_sha" ]] \
    || die "$function_name deployed CodeSha256 does not match release package"
  echo "$function_name deployed and verified: $actual_sha"
}

# Dependency order is load-bearing: persist the new visit/MMC contract before
# exposing its confidence fields through GetCustomers. Analytics is already on
# the correct encounter-ledger/local-time release and is deliberately untouched.
deploy_and_verify \
  InsertData "$INSERT_RELEASE_RECORD" "$INSERT_DATA_SNAPSHOT_REVISION"
deploy_and_verify \
  GetCustomers "$CUSTOMERS_RELEASE_RECORD" "$GET_CUSTOMERS_SNAPSHOT_REVISION"

echo "Vehicle-profile Lambda release completed for $RELEASE_SHA"
print_rollback
