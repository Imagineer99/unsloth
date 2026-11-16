set -eu
root=$(pwd)
for pair in base:4885f42602a8c4b17a8ad1f43ec3b014dd571693 head:a2b1c259fc5f2e79a28273f216b906d9e8f9eab4; do
  side=${pair%%:*}
  sha=${pair#*:}
  destination=$(mktemp -d -p "${RUNNER_TEMP:?}")
  git archive "$sha" studio/backend tests | tar -x -C "$destination"
  backend="$destination/studio/backend"
  git show a2b1c259fc5f2e79a28273f216b906d9e8f9eab4:studio/backend/tests/test_openai_auto_switch.py > "$backend/tests/alias_helpers.py"
  cp "$root/.github/pr17885/alias_probe.py" "$backend/tests/test_alias_ab.py"
  mkdir "$destination/home"
  (cd "$backend" && HOME="$destination/home" ALIAS_SIDE="$side" python -m pytest -qs tests/test_alias_ab.py)
done
