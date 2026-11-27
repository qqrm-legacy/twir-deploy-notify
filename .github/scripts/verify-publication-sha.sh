#!/usr/bin/env bash
# Usage: verify-publication-sha.sh <expected_sha> <actual_sha>
# An omitted expected_sha accepts the dispatched commit; a supplied one must
# be a well-formed commit SHA and match exactly.
set -euo pipefail

expected="${1:-}"
actual="$2"

if [[ -n "$expected" ]]; then
  if [[ ! "$expected" =~ ^[0-9a-f]{40}$ ]]; then
    echo "::error::expected_sha is not a 40-hex commit SHA" >&2
    exit 1
  fi
  if [[ "$actual" != "$expected" ]]; then
    echo "::error::Dispatched commit $actual does not match expected_sha=$expected" >&2
    exit 1
  fi
fi
