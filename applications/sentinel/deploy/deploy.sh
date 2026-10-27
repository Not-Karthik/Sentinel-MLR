#!/usr/bin/env bash
set -uo pipefail

TERRAFORM_VERSION="1.13.5"
SERVICE_NAME="sentinel"
REPOSITORY_ID="sentinel"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TF_DIR="$ROOT/terraform-cloudrun"
TOOLS_DIR="$ROOT/.tools"
VAR_FILE="$TOOLS_DIR/generated.tfvars.json"
PLAN_FILE="$TOOLS_DIR/sentinel.tfplan"

CONFIG_PATH="$SCRIPT_DIR/deploy.env"
PLAN_ONLY=false
SKIP_BUILD=false
DESTROY=false
IMAGE=""

usage() {
  echo "Usage: deploy.sh [--config PATH] [--plan-only] [--skip-build] [--destroy] [--image IMAGE]"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --config) CONFIG_PATH="$2"; shift 2 ;;
    --plan-only) PLAN_ONLY=true; shift ;;
    --skip-build) SKIP_BUILD=true; shift ;;
    --destroy) DESTROY=true; shift ;;
    --image) IMAGE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 1 ;;
  esac
done

fail() {
  echo "ERROR: $1" >&2
  exit 1
}

step() {
  echo
  echo "==> $1"
}

config_value() {
  local name="$1" default="$2" value
  value="$(grep -E "^[[:space:]]*${name}[[:space:]]*=" "$CONFIG_PATH" | tail -n 1 | tr -d '\r' | sed -E "s/^[^=]*=[[:space:]]*//; s/[[:space:]]+$//; s/^\"(.*)\"$/\1/; s/^'(.*)'$/\1/")"
  if [ -z "$value" ]; then
    echo "$default"
  else
    echo "$value"
  fi
}

to_bool() {
  case "$(echo "$1" | tr '[:upper:]' '[:lower:]')" in
    true|1|yes) echo true ;;
    false|0|no) echo false ;;
    *) fail "$2 must be true or false, got '$1'." ;;
  esac
}

json_string_list() {
  local input="$1" out="" item
  local IFS=','
  for item in $input; do
    item="$(echo "$item" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//')"
    [ -z "$item" ] && continue
    if [ -n "$out" ]; then out="$out,"; fi
    out="$out\"$item\""
  done
  echo "[$out]"
}

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

install_terraform() {
  local os arch dir zip_name base_url expected actual
  case "$(uname -s)" in
    Linux*) os="linux" ;;
    Darwin*) os="darwin" ;;
    MINGW*|MSYS*|CYGWIN*) os="windows" ;;
    *) fail "Unsupported OS $(uname -s)." ;;
  esac
  case "$(uname -m)" in
    x86_64|amd64) arch="amd64" ;;
    arm64|aarch64) arch="arm64" ;;
    *) fail "Unsupported architecture $(uname -m)." ;;
  esac
  dir="$TOOLS_DIR/terraform-$TERRAFORM_VERSION"
  TERRAFORM="$dir/terraform"
  [ "$os" = "windows" ] && TERRAFORM="$dir/terraform.exe"
  if [ -x "$TERRAFORM" ]; then
    return
  fi
  command -v curl >/dev/null 2>&1 || fail "curl is required."
  command -v unzip >/dev/null 2>&1 || fail "unzip is required."
  mkdir -p "$dir"
  base_url="https://releases.hashicorp.com/terraform/$TERRAFORM_VERSION"
  zip_name="terraform_${TERRAFORM_VERSION}_${os}_${arch}.zip"
  echo "Downloading Terraform $TERRAFORM_VERSION ($os/$arch)"
  curl -sSfL -o "$dir/$zip_name" "$base_url/$zip_name" || fail "Download failed."
  curl -sSfL -o "$dir/SHA256SUMS" "$base_url/terraform_${TERRAFORM_VERSION}_SHA256SUMS" || fail "Checksum download failed."
  expected="$(grep " $zip_name\$" "$dir/SHA256SUMS" | cut -d' ' -f1)"
  [ -n "$expected" ] || fail "No checksum found for $zip_name."
  actual="$(sha256_of "$dir/$zip_name")"
  if [ "$actual" != "$expected" ]; then
    rm -f "$dir/$zip_name"
    fail "Checksum mismatch for $zip_name."
  fi
  unzip -o -q "$dir/$zip_name" -d "$dir" || fail "Could not extract Terraform."
  rm -f "$dir/$zip_name"
  chmod +x "$TERRAFORM"
}

refresh_token() {
  local token
  token="$(gcloud auth print-access-token 2>/dev/null)" || fail "Could not get a gcloud access token. Run: gcloud auth login"
  export GOOGLE_OAUTH_ACCESS_TOKEN="$token"
}

tf() {
  refresh_token
  "$TERRAFORM" "-chdir=$TF_DIR" "$@"
}

with_retry() {
  local what="$1"
  shift
  local attempts=4 attempt
  for attempt in $(seq 1 "$attempts"); do
    if "$@"; then
      return 0
    fi
    if [ "$attempt" -lt "$attempts" ]; then
      echo "$what failed (attempt $attempt of $attempts); retrying in 60 seconds (API and IAM changes can take a few minutes to propagate)."
      sleep 60
    fi
  done
  fail "$what failed after $attempts attempts. Re-running the script is safe and resumes from here."
}

write_var_file() {
  local image_line=""
  if [ -n "${1:-}" ]; then
    image_line=",
  \"image\": \"$1\""
  fi
  mkdir -p "$TOOLS_DIR"
  cat > "$VAR_FILE" <<EOF
{
  "project_id": "$PROJECT_ID",
  "project_number": "$PROJECT_NUMBER",
  "region": "$REGION",
  "service_name": "$SERVICE_NAME",
  "repository_id": "$REPOSITORY_ID",
  "allow_unauthenticated_invoke": $ALLOW_PUBLIC,
  "invoker_members": $INVOKERS_JSON,
  "enable_api_docs": $ENABLE_DOCS,
  "allow_origins": "$ALLOW_ORIGINS",
  "media_max_mb": $MEDIA_MAX_MB,
  "deletion_protection": $DELETION_PROTECTION$image_line$EXTRA_VARS
}
EOF
}

EXTRA_VARS=""

phase_one_targets=(
  "-target=google_project_service.apis"
  "-target=google_artifact_registry_repository.sentinel"
  "-target=google_service_account.build"
  "-target=google_project_iam_member.build_roles"
)

phase_one() {
  if [ "$PLAN_ONLY" = true ]; then
    tf plan -input=false "-var-file=$VAR_FILE" "${phase_one_targets[@]}"
  else
    tf apply -input=false -auto-approve "-var-file=$VAR_FILE" "${phase_one_targets[@]}"
  fi
}

build_image() {
  gcloud builds submit "$ROOT" --project "$PROJECT_ID" --config "$SCRIPT_DIR/cloudbuild.yaml" \
    --substitutions "_IMAGE=$IMAGE_REF" \
    --service-account "projects/$PROJECT_ID/serviceAccounts/$BUILD_SA"
}

probe() {
  local url="$1"
  if [ -n "${AUTH_HEADER:-}" ]; then
    curl -sS -o "$TOOLS_DIR/probe.out" -w "%{http_code}" -H "$AUTH_HEADER" --max-time 30 "$url" 2>/dev/null
  else
    curl -sS -o "$TOOLS_DIR/probe.out" -w "%{http_code}" --max-time 30 "$url" 2>/dev/null
  fi
}

[ -f "$CONFIG_PATH" ] || fail "Config file not found: $CONFIG_PATH. Copy deploy/deploy.env.example to deploy/deploy.env and edit it."

PROJECT_ID="$(config_value PROJECT_ID '')"
if [ -z "$PROJECT_ID" ] || [ "$PROJECT_ID" = "your-gcp-project-id" ]; then
  fail "Set PROJECT_ID in the config file."
fi
if ! echo "$PROJECT_ID" | grep -Eq '^[a-z][a-z0-9-]{4,28}[a-z0-9]$'; then
  fail "PROJECT_ID '$PROJECT_ID' is not a valid project id (lowercase letters, digits and hyphens, 6 to 30 characters). Use the ID column from the console, without quotes."
fi
REGION="$(config_value REGION us-central1)"
ALLOW_PUBLIC="$(to_bool "$(config_value ALLOW_UNAUTHENTICATED_INVOKE true)" ALLOW_UNAUTHENTICATED_INVOKE)"
ENABLE_DOCS="$(to_bool "$(config_value ENABLE_API_DOCS true)" ENABLE_API_DOCS)"
DELETION_PROTECTION="$(to_bool "$(config_value DELETION_PROTECTION true)" DELETION_PROTECTION)"
ALLOW_ORIGINS="$(config_value ALLOW_ORIGINS '')"
MEDIA_MAX_MB="$(config_value MEDIA_MAX_MB 32)"
if ! echo "$MEDIA_MAX_MB" | grep -Eq '^[0-9]+$' || [ "$MEDIA_MAX_MB" -lt 1 ] || [ "$MEDIA_MAX_MB" -gt 32 ]; then
  fail "MEDIA_MAX_MB must be a whole number from 1 to 32, got '$MEDIA_MAX_MB'."
fi
INVOKERS_JSON="$(json_string_list "$(config_value INVOKER_MEMBERS '')")"

step "Checking gcloud"
command -v gcloud >/dev/null 2>&1 || fail "gcloud is not installed. Install it from https://cloud.google.com/sdk/docs/install and run: gcloud auth login"
gcloud --version >/dev/null 2>&1 || fail "gcloud failed to start. If it reports a Python error, set CLOUDSDK_PYTHON to a working Python 3.10-3.14 executable, or reinstall the Google Cloud CLI."
ACCOUNT="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | head -n 1)"
[ -n "$ACCOUNT" ] || fail "No active gcloud account. Run: gcloud auth login"
echo "Using gcloud account $ACCOUNT and project $PROJECT_ID"

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)' 2>/dev/null)"
[ -n "$PROJECT_NUMBER" ] || fail "Cannot read project '$PROJECT_ID'. Check the project id and your access."

BILLING="$(gcloud billing projects describe "$PROJECT_ID" --format='value(billingEnabled)' 2>/dev/null || true)"
if [ -n "$BILLING" ] && [ "$(echo "$BILLING" | tr '[:upper:]' '[:lower:]')" != "true" ]; then
  fail "Billing is not enabled on project '$PROJECT_ID'. Link a billing account and re-run."
fi

if [ "$ALLOW_PUBLIC" = true ]; then
  echo "WARNING: ALLOW_UNAUTHENTICATED_INVOKE=true. Anyone with the URL can call this service and use your Gemini quota."
fi

step "Preparing Terraform"
install_terraform
echo "Terraform: $TERRAFORM"

step "Enabling bootstrap APIs"
gcloud services enable serviceusage.googleapis.com cloudresourcemanager.googleapis.com storage.googleapis.com --project "$PROJECT_ID" || fail "Could not enable bootstrap APIs."

STATE_BUCKET="$PROJECT_ID-sentinel-tfstate"
if ! gcloud storage buckets describe "gs://$STATE_BUCKET" --project "$PROJECT_ID" >/dev/null 2>&1; then
  step "Creating Terraform state bucket gs://$STATE_BUCKET"
  gcloud storage buckets create "gs://$STATE_BUCKET" --project "$PROJECT_ID" --location "$REGION" --uniform-bucket-level-access --public-access-prevention || fail "Could not create the state bucket."
  gcloud storage buckets update "gs://$STATE_BUCKET" --project "$PROJECT_ID" --versioning || fail "Could not enable versioning on the state bucket."
fi

IMAGE_BASE="$REGION-docker.pkg.dev/$PROJECT_ID/$REPOSITORY_ID"
BUILD_SA="$SERVICE_NAME-build@$PROJECT_ID.iam.gserviceaccount.com"

step "Initializing Terraform"
write_var_file ""
tf init -input=false -reconfigure "-backend-config=bucket=$STATE_BUCKET" "-backend-config=prefix=sentinel-cloudrun" || fail "terraform init failed."
tf validate || fail "terraform validate failed."

if [ "$DESTROY" = true ]; then
  step "Destroying the Sentinel deployment"
  echo "This deletes the Cloud Run service, the media bucket and its contents, the Artifact Registry repository and the service accounts in project $PROJECT_ID."
  read -r -p "Type the project id ($PROJECT_ID) to confirm: " ANSWER
  [ "$ANSWER" = "$PROJECT_ID" ] || fail "Confirmation did not match. Nothing was destroyed."
  DELETION_PROTECTION=false
  EXTRA_VARS=",
  \"bucket_force_destroy\": true"
  write_var_file "$IMAGE_BASE/$SERVICE_NAME:destroy"
  tf apply -input=false -auto-approve "-var-file=$VAR_FILE" -target=google_cloud_run_v2_service.sentinel -target=google_storage_bucket.media || fail "Could not lift deletion protection."
  tf destroy -input=false -auto-approve "-var-file=$VAR_FILE" || fail "terraform destroy failed."
  echo "Destroyed. The state bucket and the Cloud Build source bucket remain."
  exit 0
fi

step "Phase 1: APIs, Artifact Registry and build identity"
with_retry "Phase 1" phase_one

if [ -n "$IMAGE" ]; then
  IMAGE_REF="$IMAGE"
elif [ "$PLAN_ONLY" = true ]; then
  IMAGE_REF="$IMAGE_BASE/$SERVICE_NAME:plan-only"
elif [ "$SKIP_BUILD" = true ]; then
  step "Reusing the currently deployed image"
  IMAGE_REF="$(gcloud run services describe "$SERVICE_NAME" --project "$PROJECT_ID" --region "$REGION" --format='value(spec.template.spec.containers[0].image)' 2>/dev/null)"
  [ -n "$IMAGE_REF" ] || fail "No deployed image found. Run without --skip-build first."
else
  step "Building the image with Cloud Build"
  IMAGE_REF="$IMAGE_BASE/$SERVICE_NAME:$(date +%Y%m%d-%H%M%S)"
  with_retry "Cloud Build" build_image
fi
echo "Image: $IMAGE_REF"

step "Phase 2: planning the full deployment"
write_var_file "$IMAGE_REF"
tf plan -input=false "-var-file=$VAR_FILE" "-out=$PLAN_FILE" || fail "terraform plan failed."

if [ "$PLAN_ONLY" = true ]; then
  echo "Plan only: nothing was applied."
  exit 0
fi

step "Phase 2: applying"
with_retry "terraform apply" tf apply -input=false "$PLAN_FILE"

step "Reading outputs"
SERVICE_URL="$(tf output -raw service_url)"
EXPECTED_URL="$(tf output -raw expected_app_url)"
DOCS_URL="$SERVICE_URL/docs"
CARD_URL="$SERVICE_URL/a2a/app/.well-known/agent-card.json"

step "Smoke test"
AUTH_HEADER=""
if [ "$ALLOW_PUBLIC" != true ]; then
  IDENTITY="$(gcloud auth print-identity-token 2>/dev/null || true)"
  [ -n "$IDENTITY" ] && AUTH_HEADER="Authorization: Bearer $IDENTITY"
fi
HEALTHY=false
for attempt in 1 2 3 4 5 6 7 8; do
  if [ "$(probe "$SERVICE_URL/health")" = "200" ]; then
    HEALTHY=true
    break
  fi
  sleep 10
done
[ "$HEALTHY" = true ] || fail "Health check failed at $SERVICE_URL/health. Check Cloud Run logs for service $SERVICE_NAME."
echo "health: OK"

[ "$(probe "$CARD_URL")" = "200" ] || fail "Agent card not reachable at $CARD_URL."
if ! grep -q "\"url\": *\"$EXPECTED_URL/a2a/app\"" "$TOOLS_DIR/probe.out"; then
  fail "Agent card does not advertise $EXPECTED_URL/a2a/app. Check APP_URL and the service URL."
fi
echo "agent card: OK ($EXPECTED_URL/a2a/app)"

if [ "$ENABLE_DOCS" = true ]; then
  if [ "$(probe "$DOCS_URL")" = "200" ]; then
    echo "docs: OK"
  else
    echo "WARNING: /docs did not return 200."
  fi
fi

echo
echo "Deployment complete."
echo "Service URL : $SERVICE_URL"
echo "Swagger docs: $DOCS_URL"
echo "Agent card  : $CARD_URL"
echo "Image       : $IMAGE_REF"
if [ "$ALLOW_PUBLIC" != true ]; then
  echo
  echo "The service is IAM-protected. To call it from this machine:"
  echo "  gcloud run services proxy $SERVICE_NAME --project $PROJECT_ID --region $REGION --port 8080"
  echo "  then use http://localhost:8080 as base_url in Postman."
fi
echo
echo "Postman collection: deploy/postman. Details: deploy/README.md"
