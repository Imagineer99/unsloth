# SPDX-License-Identifier: AGPL-3.0-only
"""Provision independent sides, execute the same probe, save sanitized results."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

BASE = '191b69c12b4434b5247f1fd7a455b4a760b169ae'
HEAD = 'b675a9d70a9514bbdcb95b98fe41242313c0707b'
p = argparse.ArgumentParser()
p.add_argument('--base', default='sources/base')
p.add_argument('--head', default='sources/head')
p.add_argument('--work', default='temp/ab')
p.add_argument('--all-python-platforms', action='store_true')
args = p.parse_args()
root = Path(args.work).resolve()
root.mkdir(parents=True, exist_ok=True)
out = root / 'results'
out.mkdir(exist_ok=True)
codes = {}
for side, sha, other in [('base', BASE, args.head), ('head', HEAD, args.base)]:
    source = Path(getattr(args, side)).resolve()
    actual = subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'], text=True).strip()
    assert actual == sha, (side, actual)
    side_root = root / side
    side_root.mkdir(exist_ok=True)
    env = dict(os.environ, UV_CACHE_DIR=str(side_root / 'cache'), PYTHONNOUSERSITE='1')
    for key in ['GH_TOKEN','GITHUB_TOKEN','HF_TOKEN','HUGGING_FACE_HUB_TOKEN','OPENAI_API_KEY']:
        env.pop(key, None)
    venv = side_root / 'venv'
    subprocess.run(['uv','venv','--python',sys.executable,str(venv)], env=env, check=True)
    py = venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    subprocess.run(['uv','pip','install','--python',str(py),'packaging==26.3','pip==26.2.1'], env=env, check=True)
    cmd = [str(py), str(Path(__file__).with_name('probe.py')), '--source', str(source), '--other', str(Path(other).resolve()), '--side',side,'--out',str(out)]
    if args.all_python_platforms: cmd.append('--all-python-platforms')
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=300)
    text = result.stdout + '\n' + result.stderr
    # Replace local paths before log upload. Do not publish environment dumps.
    for private, label in [(str(root),'<WORK>'),(str(source),'<SOURCE>'),(str(Path.home()),'<HOME>'),(str(Path.cwd()),'<WORKSPACE>')]:
        text = text.replace(private,label).replace(private.replace('\\','/'),label)
    (out / (side + '.log')).write_text(text, encoding='utf-8')
    print(text, flush=True)
    codes[side] = result.returncode
(out / 'summary.json').write_text(json.dumps({'base':BASE,'head':HEAD,'exit_codes':codes},indent=2)+'\n')
summary = os.environ.get('GITHUB_STEP_SUMMARY')
if summary:
    with open(summary,'a',encoding='utf-8') as f:
        f.write(f'## PR 10649 targeted A/B\n\nBase: `{BASE}`\n\nHead: `{HEAD}`\n\n')
        for side in ['base','head']:
            path = out / (side+'.json')
            if not path.exists():
                f.write(f'- **{side}: harness did not produce results.**\n')
                continue
            data = json.loads(path.read_text())
            passed = sum(r['status']=='pass' for r in data['results'])
            f.write(f'- {side}: {passed}/{len(data["results"])} assertions passed.\n')
            for row in data['results']:
                if row['status']!='pass': f.write(f'  - FAIL `{row["case"]}`: actual `{row["actual"]}`, expected `{row["expected"]}`\n')
        f.write('\nLocal fixture wheels and metadata; no GPU kernels, full Studio launch, or browsers.\n')
sys.exit(1 if any(codes.values()) else 0)
