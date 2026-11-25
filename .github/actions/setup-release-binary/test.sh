#!/usr/bin/env bash
# Offline regression tests for the production installer (setup.sh).
# The real script is executed; only curl, date and sleep are mocked so that
# release states and retry timeouts are deterministic and no network is used.
set -euo pipefail

setup="$(cd "$(dirname "$0")" && pwd)/setup.sh"
publication_check="$(cd "$(dirname "$0")/../.." && pwd)/scripts/verify-publication-sha.sh"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cd "$work"
mkdir -p fixtures bin
export FIXTURES="$work/fixtures"
export CLOCK_FILE="$work/clock"
export MANIFEST_CALLS="$work/manifest_calls"
export BINARY_CALLS="$work/binary_calls"
export PATH="$work/bin:$PATH"
export RELEASE_WAIT_SECONDS=0

# A controllable clock: mock sleep advances it, setup.sh reads it via date.
cat > bin/date <<'MOCK'
#!/usr/bin/env bash
cat "$CLOCK_FILE"
MOCK

cat > bin/sleep <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail
step="${1:-1}"
echo "$(( $(cat "$CLOCK_FILE") + step ))" > "$CLOCK_FILE"
MOCK

# Serve fixtures. manifest.N overrides the manifest on attempt N, which lets
# us simulate a release being published while the installer retries.
cat > bin/curl <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail
out=""
url=""
while (($#)); do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    --max-time) shift 2 ;;
    -L|--location|--fail|--silent|--show-error) shift ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
asset="${url##*/}"
printf '%s\n' "$asset" >> "$FIXTURES/downloads"
if [[ "$asset" == delivery-manifest.json ]]; then
  n="$(cat "$MANIFEST_CALLS" 2>/dev/null || echo 0)"
  n=$((n + 1))
  echo "$n" > "$MANIFEST_CALLS"
  src="$FIXTURES/manifest.$n"
  [[ -f "$src" ]] || src="$FIXTURES/delivery-manifest.json"
else
  n="$(cat "$BINARY_CALLS" 2>/dev/null || echo 0)"
  n=$((n + 1))
  echo "$n" > "$BINARY_CALLS"
  src="$FIXTURES/$asset.$n"
  [[ -f "$src" ]] || src="$FIXTURES/$asset"
fi
[[ -f "$src" ]] || { echo "missing fixture asset: $asset" >&2; exit 22; }
cp "$src" "$out"
MOCK
chmod +x bin/date bin/sleep bin/curl

sha=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
older=1111111111111111111111111111111111111111
newer=2222222222222222222222222222222222222222
printf 'test binary\n' > fixtures/bot
hash="$(sha256sum fixtures/bot | cut -d' ' -f1)"

manifest() {
  jq -n --arg source_sha "$1" --arg sha256 "$2" --argjson protocol "$3" \
    '{source_sha:$source_sha,sha256:$sha256,protocol:$protocol}' \
    > fixtures/delivery-manifest.json
}

reset() {
  rm -f bot delivery-manifest.json fixtures/downloads
  rm -f "$MANIFEST_CALLS" "$BINARY_CALLS"
  rm -f fixtures/manifest.1 fixtures/manifest.2 fixtures/bot.1 fixtures/bot.2
  echo 0 > "$CLOCK_FILE"
  export RELEASE_WAIT_SECONDS=0
}

reject() {
  local checked_out="$1"
  reset
  if bash "$setup" example/repo bot "$checked_out"; then
    echo "Unexpected acceptance: $2" >&2
    exit 1
  fi
  test ! -e bot
  test ! -e delivery-manifest.json
}

accept() {
  local checked_out="$1"
  reset
  bash "$setup" example/repo bot "$checked_out"
  test -x bot
  cmp -s bot fixtures/bot
  cmp -s delivery-manifest.json fixtures/delivery-manifest.json
}

# Correct source SHA and checksum.
manifest "$sha" "$hash" 2
accept "$sha" "matching release"

# Documentation-only commit: its exact SHA has a published release too.
doc_sha=cccccccccccccccccccccccccccccccccccccccc
manifest "$doc_sha" "$hash" 2
accept "$doc_sha" "documentation-only commit"

# Older and newer source SHA must never be accepted.
manifest "$older" "$hash" 2
reject "$sha" "older source SHA"
[[ "$(cat fixtures/downloads)" == delivery-manifest.json ]] ||
  { echo "Binary downloaded for a stale manifest" >&2; exit 1; }

manifest "$newer" "$hash" 2
reject "$sha" "newer source SHA"

# Manual dispatch from a non-main branch: no release exists for that SHA.
manifest "$sha" "$hash" 2
reject "$older" "manual dispatch from a non-main branch"

# Missing, malformed and unsupported-protocol manifests.
rm -f fixtures/delivery-manifest.json
reject "$sha" "missing manifest"
printf 'not json' > fixtures/delivery-manifest.json
reject "$sha" "malformed manifest"
manifest "$sha" "$hash" 1
reject "$sha" "unsupported protocol"
jq -n --arg hash "$hash" '{sha256:$hash,protocol:2}' > fixtures/delivery-manifest.json
reject "$sha" "missing source_sha"

# Incorrect checksum: the binary is downloaded but never installed.
manifest "$sha" "${hash:0:63}" 2
reject "$sha" "malformed checksum"
manifest "$sha" "$(printf '%064d' 0)" 2
reject "$sha" "incorrect checksum"
[[ "$(cat fixtures/downloads)" == $'delivery-manifest.json\nbot' ]] ||
  { echo "Checksum path was not exercised" >&2; exit 1; }

# Missing binary.
manifest "$sha" "$hash" 2
rm -f fixtures/bot
reject "$sha" "missing binary"
printf 'test binary\n' > fixtures/bot

# Invalid inputs fail without contacting the release.
reject deadbeef "malformed checked-out SHA"
test ! -e fixtures/downloads
reset
manifest "$sha" "$hash" 2
if RELEASE_WAIT_SECONDS=abc bash "$setup" example/repo bot "$sha"; then
  echo "Unexpected acceptance with invalid RELEASE_WAIT_SECONDS" >&2
  exit 1
fi
test ! -e bot
test ! -e fixtures/downloads

# Concurrent publication: the first manifest is stale, the second is correct.
reset
manifest "$older" "$hash" 2
cp fixtures/delivery-manifest.json fixtures/manifest.1
manifest "$sha" "$hash" 2
cp fixtures/delivery-manifest.json fixtures/manifest.2
RELEASE_WAIT_SECONDS=60 bash "$setup" example/repo bot "$sha"
test -x bot
cmp -s bot fixtures/bot
cmp -s delivery-manifest.json fixtures/manifest.2
[[ "$(cat fixtures/downloads)" == $'delivery-manifest.json\ndelivery-manifest.json\nbot' ]] ||
  { echo "Retry did not re-fetch the manifest first" >&2; exit 1; }

# Concurrent publication: a valid manifest with a transiently stale binary
# must be retried rather than rejected outright.
reset
manifest "$sha" "$hash" 2
printf 'stale binary\n' > fixtures/bot.1
printf 'test binary\n' > fixtures/bot.2
RELEASE_WAIT_SECONDS=60 bash "$setup" example/repo bot "$sha"
test -x bot
cmp -s bot fixtures/bot.2
[[ "$(cat fixtures/downloads)" == $'delivery-manifest.json\nbot\ndelivery-manifest.json\nbot' ]] ||
  { echo "Checksum mismatch was not retried" >&2; exit 1; }

# Retry timeout: persistently stale assets fail closed after the deadline.
reset
manifest "$older" "$hash" 2
RELEASE_WAIT_SECONDS=1 bash "$setup" example/repo bot "$sha" && {
  echo "Unexpected acceptance after retry timeout" >&2
  exit 1
}
test ! -e bot
test ! -e delivery-manifest.json
[[ "$(cat fixtures/downloads)" == $'delivery-manifest.json\ndelivery-manifest.json' ]] ||
  { echo "Expected one retry before the timeout" >&2; exit 1; }

# Publication controller: expected_sha is optional but must match when given.
bash "$publication_check" "" "$sha"
bash "$publication_check" "$sha" "$sha"
if bash "$publication_check" "$older" "$sha"; then
  echo "Mismatched expected_sha was accepted" >&2
  exit 1
fi
if bash "$publication_check" deadbeef "$sha"; then
  echo "Malformed expected_sha was accepted" >&2
  exit 1
fi

printf 'Release selection regression tests passed.\n'
