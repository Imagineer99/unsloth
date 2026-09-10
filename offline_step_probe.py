# SPDX-License-Identifier: AGPL-3.0-only
"""Exercise real installer gates and uv against tiny locally generated wheels."""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT = Path(os.environ.get('PR10649_CHECKOUT_ROOT', str(Path(__file__).resolve().parent)))
MODE = sys.argv[1] if len(sys.argv) > 1 else 'head'
HEAD = ROOT / MODE
WHEELS = ROOT / 'probe-wheels'
REQ = ROOT / 'probe-requirements'
WHEELS.mkdir(exist_ok=True)
(REQ / 'single-env').mkdir(parents=True, exist_ok=True)

def wheel(name, version, dependency=None):
    info = f'{name}-{version}.dist-info'
    contents = {
        f'{name}/__init__.py': ('import review_child\n' if dependency else 'VALUE = 42\n').encode(),
        f'{info}/METADATA': (f'Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n' +
                             (f'Requires-Dist: {dependency}\n' if dependency else '')).encode(),
        f'{info}/WHEEL': b'Wheel-Version: 1.0\nGenerator: local-review\nRoot-Is-Purelib: true\nTag: py3-none-any\n',
    }
    rows = []
    for name_in_wheel, data in contents.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip('=')
        rows.append(f'{name_in_wheel},sha256={digest},{len(data)}')
    contents[f'{info}/RECORD'] = ('\n'.join(rows) + f'\n{info}/RECORD,,\n').encode()
    with zipfile.ZipFile(WHEELS / f'{name}-{version}-py3-none-any.whl', 'w') as out:
        for path, data in contents.items(): out.writestr(path, data)

wheel('review_child', '1.0')
wheel('review_child', '2.0')
wheel('review_parent', '1.0', 'review_child>=1')
req = REQ / 'studio.txt'
req.write_text('review_parent==1.0\n')
constraints = REQ / 'single-env/constraints.txt'
constraints.write_text('review_child==1.0\n')
os.environ['UV_NO_INDEX'] = '1'
os.environ['UV_FIND_LINKS'] = str(WHEELS)
os.environ['PIP_NO_INDEX'] = '1'
os.environ['PIP_FIND_LINKS'] = str(WHEELS)
sys.path.insert(0, str(HEAD / 'studio'))
import install_python_stack as stack
stack.REQ_ROOT = REQ
stack.CONSTRAINTS = constraints
stack.NO_TORCH = False
stack.IS_WINDOWS = False
stack.IS_MAC_ARM = False
stack.PLATFORM_LACKS_TORCHCODEC_WHEEL = False
stack.USE_UV = True
stack.UV_NEEDS_SYSTEM = False
stack._TOTAL = 8

results = []
def evidence():
    if MODE == 'base': return
    stack._PASS_EVIDENCE = {
        'pass_inputs': stack.install_manifest.pass_input_digests(REQ),
        'step_results': {'studio.txt': 'ran'},
    }
    stack._CLOSURE_INDEX_CACHE = None
    stack._CONSTRAINTS_CACHE = None

def apply(label, expected_skip):
    if MODE == 'base': expected_skip = False
    skip = stack._skip_step(req, label, no_deps=False) if MODE == 'head' else False
    assert skip == expected_skip, (label, skip)
    if not skip: stack.pip_install(label, '--no-cache-dir', req=req)
    subprocess.run([sys.executable, '-c', 'import review_parent; import review_child; assert review_child.VALUE == 42'], check=True)
    results.append({'case': label, 'skipped': skip, 'import_ok': True})

apply('cold install from local wheels', False)
evidence()
apply('unchanged install', True)
subprocess.run(['uv', 'pip', 'uninstall', '--python', sys.executable, 'review_child'], check=True)
broken = subprocess.run([sys.executable, '-c', 'import review_parent'], capture_output=True)
assert broken.returncode != 0 and b'ModuleNotFoundError' in broken.stderr
stack._CLOSURE_INDEX_CACHE = None  # a new dependency pass
apply('missing transitive dependency repaired', False)
evidence()
apply('settled after repair', True)
constraints.write_text('review_child==2.0\n')
apply('changed constraint applied', False)
from importlib.metadata import version
assert version('review_child') == '2.0'
evidence()
apply('settled after constraint change', True)
req.write_text('review_parent==1.0\n# changed input bytes\n')
apply('changed input bytes rerun', False)
evidence()
apply('settled after input change', True)
(ROOT / f'offline-step-{MODE}-results.json').write_text(json.dumps(results, indent=2))
print(json.dumps(results, indent=2))
