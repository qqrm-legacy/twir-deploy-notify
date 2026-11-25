#!/usr/bin/env bash
set -euo pipefail

# Usage: setup.sh <repository> <binary_name> <source_sha>
# Installs only a release binary whose manifest matches the checked-out commit.
repository="$1"
binary_name="$2"
source_sha="$3"

if [[ ! "$source_sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "::error::Invalid checked-out source SHA: refusing to run." >&2
  exit 1
fi

wait_seconds="${RELEASE_WAIT_SECONDS:-900}"
if [[ ! "$wait_seconds" =~ ^[0-9]+$ ]]; then
  echo "::error::Invalid RELEASE_WAIT_SECONDS: refusing to run." >&2
  exit 1
fi

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT
manifest="$tmp_dir/delivery-manifest.json"
binary="$tmp_dir/$binary_name"
base_url="https://github.com/$repository/releases/latest/download"

now() { date +%s; }
download() {
  curl --fail --silent --show-error --location --max-time 20 -o "$1" "$2"
}

# `latest` is mutable: during publication the binary and manifest can briefly
# disagree. Retry until they describe the exact checked-out commit, then fail
# closed. Nothing is copied into the workspace until every check passes.
deadline=$(( $(now) + wait_seconds ))
while :; do
  # Validate the manifest before downloading a binary for a different commit.
  if download "$manifest" "$base_url/delivery-manifest.json" &&
     jq -e --arg source_sha "$source_sha" '
       .protocol == 2 and .source_sha == $source_sha and
       (.sha256 | type == "string" and test("^[0-9a-f]{64}$"))
     ' "$manifest" >/dev/null 2>&1 &&
     download "$binary" "$base_url/$binary_name"; then
    expected="$(jq -r '.sha256' "$manifest")"
    actual="$(sha256sum "$binary" | cut -d' ' -f1)"
    if [[ "$actual" == "$expected" ]]; then
      chmod +x "$binary"
      mv -f -- "$binary" "$binary_name"
      mv -f -- "$manifest" delivery-manifest.json
      exit 0
    fi
  fi

  remaining=$(( deadline - $(now) ))
  if (( remaining <= 0 )); then
    echo "::error::No verified release binary for source_sha=$source_sha; refusing to run." >&2
    exit 1
  fi
  sleep "$(( remaining < 30 ? remaining : 30 ))"
done
