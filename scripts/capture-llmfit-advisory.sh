#!/bin/sh
set -eu

usage() {
  echo "usage: $0 OUTPUT_DIR [MODEL CONTEXT_LENGTH [QUANT]]" >&2
  echo "captures a pinned llmfit advisory; it does not download or deploy a model" >&2
}

if [ "$#" -ne 1 ] && [ "$#" -ne 3 ] && [ "$#" -ne 4 ]; then
  usage
  exit 2
fi

output_dir=$1
expected_version=1.1.9
expected_platform=Darwin/arm64
expected_sha256=04c0799a258db2d96a221214abdc4c10e8e88e3bdc1337f11877272f330efa46
recommend_limit=${LLMFIT_RECOMMEND_LIMIT:-5}

case "$recommend_limit" in
  ''|*[!0-9]*) echo "LLMFIT_RECOMMEND_LIMIT must be a positive integer" >&2; exit 2 ;;
  0) echo "LLMFIT_RECOMMEND_LIMIT must be a positive integer" >&2; exit 2 ;;
esac

llmfit_bin=$(command -v llmfit 2>/dev/null || true)
if [ -z "$llmfit_bin" ]; then
  echo "llmfit is required" >&2
  exit 1
fi
if ! command -v jq >/dev/null 2>&1; then
  echo "jq is required to validate llmfit JSON" >&2
  exit 1
fi

platform=$(uname -s)/$(uname -m)
if [ "$platform" != "$expected_platform" ]; then
  echo "no reviewed llmfit binary pin for $platform; expected $expected_platform" >&2
  exit 1
fi

binary_sha256=$(shasum -a 256 "$llmfit_bin" | awk '{print $1}')
if [ "$binary_sha256" != "$expected_sha256" ]; then
  echo "refusing unreviewed llmfit binary: SHA-256 $binary_sha256; expected $expected_sha256" >&2
  exit 1
fi

actual_version=$("$llmfit_bin" --version | awk '{print $2}')
if [ "$actual_version" != "$expected_version" ]; then
  echo "refusing llmfit $actual_version; expected pinned version $expected_version" >&2
  exit 1
fi

if [ -e "$output_dir" ]; then
  echo "refusing to overwrite $output_dir" >&2
  exit 1
fi

umask 077
mkdir "$output_dir"
complete=false
cleanup() {
  if [ "$complete" != true ]; then
    rm -rf "$output_dir"
  fi
}
trap cleanup 0 1 2 15

"$llmfit_bin" --no-dashboard --json system >"$output_dir/system.json"
"$llmfit_bin" recommend --no-dashboard --json --limit "$recommend_limit" >"$output_dir/recommendation.json"

if [ "$#" -ge 3 ]; then
  model=$2
  context_length=$3
  quant=${4:-}
  case "$context_length" in
    ''|*[!0-9]*) echo "CONTEXT_LENGTH must be a positive integer" >&2; exit 2 ;;
    0) echo "CONTEXT_LENGTH must be a positive integer" >&2; exit 2 ;;
  esac
  if [ -n "$quant" ]; then
    "$llmfit_bin" plan "$model" --no-dashboard --context "$context_length" --quant "$quant" --json >"$output_dir/plan.json"
  else
    "$llmfit_bin" plan "$model" --no-dashboard --context "$context_length" --json >"$output_dir/plan.json"
  fi
fi

for report in "$output_dir"/*.json; do
  jq -e . "$report" >/dev/null
done

{
  printf 'evidence_class=development-advisory\n'
  printf 'authority=non-authoritative\n'
  printf 'tool=llmfit\n'
  printf 'version=%s\n' "$actual_version"
  printf 'executable=%s\n' "$llmfit_bin"
  printf 'executable_sha256=%s\n' "$binary_sha256"
  printf 'recommend_limit=%s\n' "$recommend_limit"
  if [ "$#" -ge 3 ]; then
    printf 'plan_model=%s\n' "$model"
    printf 'plan_context_length=%s\n' "$context_length"
    printf 'plan_quant=%s\n' "${quant:-default}"
  else
    printf 'plan=not-requested\n'
  fi
  printf 'captured_at_utc=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  printf 'sharing=disabled\n'
} >"$output_dir/provenance.txt"

(cd "$output_dir" && shasum -a 256 ./*.json provenance.txt >SHA256SUMS)
complete=true

echo "captured advisory evidence in $output_dir"
echo "llm-fit recommends; BlockOps validates the approved deployment envelope"
