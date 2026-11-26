set -euo pipefail
side=$1
sha=$2
port=$3
test "$(git rev-parse HEAD)" = "$sha"
echo "Testing $side $sha"
export UNSLOTH_STUDIO_HOME="$RUNNER_TEMP/sidebar-$side"
export UNSLOTH_SKIP_AUTOSTART=1 UNSLOTH_DISABLE_UPDATE_CHECK=1 UNSLOTH_API_ONLY=1
bash install.sh --local --no-torch > "$RUNNER_TEMP/install-$side.log" 2>&1 || {
  grep -iE 'error:|failed to|no space left' "$RUNNER_TEMP/install-$side.log" | tail -12
  exit 1
}
setsid "$UNSLOTH_STUDIO_HOME/unsloth_studio/bin/unsloth" studio -H 127.0.0.1 -p "$port" > "$RUNNER_TEMP/server-$side.log" 2>&1 &
pid=$!
trap 'kill -- "-$pid" 2>/dev/null || true' EXIT
bash .github/scripts/wait-for-health.sh --port "$port"
python "$GITHUB_WORKSPACE/.github/sidebar-ab/probe.py" "$side" "http://127.0.0.1:$port"
