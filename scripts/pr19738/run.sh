set -euo pipefail
driver="$GITHUB_WORKSPACE/scripts/pr19738"
work="$RUNNER_TEMP/ab"
mkdir -p "$work"
cd "$work"
source "$RUNNER_TEMP/ab-venv/bin/activate"
checkout() {
    git init -q "$1"
    git -C "$1" fetch -q --depth=1 "https://github.com/$2.git" "$3"
    git -C "$1" checkout -q --detach FETCH_HEAD
    test "$(git -C "$1" rev-parse HEAD)" = "$3"
}
checkout before unslothai/unsloth "$BEFORE"
checkout after unslothai/unsloth "$AFTER"
checkout zoo unslothai/unsloth-zoo "$ZOO"
uv pip install --override after/studio/backend/requirements/single-env/overrides-darwin-arm64.txt -r after/studio/backend/requirements/studio.txt 'mlx==0.32.4' 'mlx-lm==0.32.2' 'mlx-vlm==0.8.1' './zoo[core]' 'playwright' 'pillow' 'python-multipart'
python -m playwright install chromium
export HF_HUB_CACHE="$work/hub"
python - <<'PY'
from pathlib import Path
from huggingface_hub import snapshot_download
import os
snapshot=Path(snapshot_download(os.environ['MODEL_ID'],revision=os.environ['MODEL_SHA'],allow_patterns=['*.json','*.safetensors','*.jinja','*.txt','*.model']))
assert snapshot.name==os.environ['MODEL_SHA']
refs=snapshot.parents[1]/'refs'
refs.mkdir(exist_ok=True)
(refs/'main').write_text(snapshot.name)
PY
export HF_HUB_OFFLINE=1
for side in before after; do
    uv pip install --no-deps -e "$work/$side"
    (cd "$side/studio/frontend" && npm ci --no-audit --no-fund && npx --no-install vite build)
    python "$driver/scene.py" "$side" "$work"
done
python - <<'PY'
import json
from pathlib import Path
a,b=[json.loads(Path(s+'.json').read_text()) for s in ('before','after')]
assert a['reply_sha256']==b['reply_sha256'] and a['finish']==b['finish'], 'Reply or finish reason changed'
print('PASS: verified identical reply and finish reason, with the expected UI difference.')
PY
