# SPDX-License-Identifier: AGPL-3.0-only
"""Pinned-source installer decisions; local fixture wheels, no GPU computation."""
import argparse
import ast
import base64
import contextlib
import hashlib
import importlib
import importlib.metadata as metadata
import importlib.util
import inspect
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import textwrap
from unittest.mock import patch
import zipfile

p = argparse.ArgumentParser()
p.add_argument('--source', required=True)
p.add_argument('--other', required=True)
p.add_argument('--side', choices=['base', 'head'], required=True)
p.add_argument('--out', required=True)
p.add_argument('--all-python-platforms', action='store_true')
args = p.parse_args()
source = Path(args.source).resolve()
out = Path(args.out).resolve()
out.mkdir(parents=True, exist_ok=True)
work = out.parent / ('fixtures-' + args.side)
work.mkdir(exist_ok=True)
os.environ['TMPDIR'] = str(work)
os.environ['TMP'] = str(work)
os.environ['TEMP'] = str(work)
os.environ['SKIP_STUDIO_BASE'] = '1'
for key in ['STUDIO_LOCAL_REPO', 'UNSLOTH_CI_SOURCE_OVERLAY', 'STUDIO_PACKAGE_NAME',
            'UV_OVERRIDE', 'UV_CONSTRAINT', 'UV_BUILD_CONSTRAINT', 'PIP_CONSTRAINT',
            'PIP_NO_DEPS', 'UV_NO_DEPS', 'UNSLOTH_STUDIO_FULL_DEPS']:
    os.environ.pop(key, None)
sys.path.insert(0, str(source / 'studio'))
import install_python_stack as stack
im = stack.install_manifest
stack.NO_TORCH = True
stack.USE_UV = True
stack.UV_NEEDS_SYSTEM = False
stack._TOTAL = 100
stack._STEP = 0
stack.IS_MAC_ARM = False  # no MLX resolver overrides for the local-wheel fixture
stack.PLATFORM_LACKS_TORCHCODEC_WHEEL = False
reqroot = work / 'requirements'
(reqroot / 'single-env').mkdir(parents=True, exist_ok=True)
for name in getattr(im, 'PASS_INPUT_FILES', im.TRACKED_REQUIREMENT_FILES):
    target = reqroot / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('# fixture input\n')
req = reqroot / 'studio.txt'
constraints = reqroot / 'single-env/constraints.txt'
req.write_text('review_parent==1.0\n')
constraints.write_text('review_child==1.0\n')
stack.REQ_ROOT = reqroot
stack.SINGLE_ENV = reqroot / 'single-env'
stack.CONSTRAINTS = constraints
wheelroot = work / 'wheels'
wheelroot.mkdir(exist_ok=True)
for key in ['UV_FIND_LINKS', 'PIP_FIND_LINKS']:
    os.environ[key] = str(wheelroot)
os.environ['UV_NO_INDEX'] = '1'
os.environ['PIP_NO_INDEX'] = '1'

results = []
def check(name, actual, expected, category='compatibility'):
    result = {'case': name, 'category': category, 'actual': actual, 'expected': expected,
              'status': 'pass' if actual == expected else 'fail'}
    results.append(result)
    print(json.dumps(result), flush=True)

def phase(name, fn):
    try:
        fn()
    except Exception as exc:
        # Avoid environment/command dumps in public artifacts.
        check(name + '_harness_completed', type(exc).__name__, 'no exception', 'harness')
        print('HARNESS ERROR:', name, str(exc)[:400], flush=True)

def wheel(name, version, dependency=None):
    info = f'{name}-{version}.dist-info'
    files = {
        f'{name}/__init__.py': ('import review_child\n' if dependency else 'VALUE = 42\n').encode(),
        f'{info}/METADATA': (f'Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n' +
                            (f'Requires-Dist: {dependency}\n' if dependency else '')).encode(),
        f'{info}/WHEEL': b'Wheel-Version: 1.0\nGenerator: isolated-review\nRoot-Is-Purelib: true\nTag: py3-none-any\n',
    }
    rows = []
    for path, data in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip('=')
        rows.append(f'{path},sha256={digest},{len(data)}')
    files[f'{info}/RECORD'] = ('\n'.join(rows) + f'\n{info}/RECORD,,\n').encode()
    dest = wheelroot / f'{name}-{version}-py3-none-any.whl'
    with zipfile.ZipFile(dest, 'w') as z:
        for path, data in files.items(): z.writestr(path, data)
    return dest

def uv(*argv):
    return subprocess.run(['uv', 'pip', *argv], check=True, capture_output=True, text=True, timeout=90)

def reset_caches():
    importlib.invalidate_caches()
    stack._CLOSURE_INDEX_CACHE = None
    stack._CONSTRAINTS_CACHE = None

def save_manifest():
    extra = {}
    if args.side == 'head':
        extra = {'extra': {'pass_inputs': im.pass_input_digests(reqroot),
                          'step_results': {'studio.txt': 'ran'},
                          'installer_python_tag': stack._installer_python_tag()}}
    assert im.write_manifest(req_root=reqroot, no_torch=True, **extra)

def plan():
    reset_caches()
    if args.side == 'head':
        stack._PASS_EVIDENCE = stack._plan_pass('unsloth', '', '')
        return stack._PASS_EVIDENCE

def fixture_dist(name, version='1.0', module=None):
    """Metadata and RECORD fixture in this disposable interpreter's site-packages."""
    site = Path(sysconfig.get_paths()['purelib'])
    module = module or name.replace('-', '_')
    package = site / module
    package.mkdir(exist_ok=True)
    (package / '__init__.py').write_text('')
    payload = package / 'internal.py'
    payload.write_bytes(b'VALUE = 42\n')
    info = site / f'{name.replace("-", "_")}-{version}.dist-info'
    info.mkdir(exist_ok=True)
    (info / 'METADATA').write_text(f'Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n')
    record = info / 'RECORD'
    record.write_text(f'{module}/__init__.py,,0\n{module}/internal.py,,11\n')
    importlib.invalidate_caches()
    return info, payload, record

def requirements_probe():
    # Minimal synthetic core wheels let the actual manifest deep verifier run.
    cores = [wheel('unsloth', '1.0'), wheel('unsloth_zoo', '1.0')]
    wheel('review_parent', '1.0', 'review_child>=1')
    wheel('review_child', '1.0')
    wheel('review_child', '2.0')
    uv('install', '--python', sys.executable, '--no-deps', *map(str, cores))
    # Execute the same named step directly from each source's real function body.
    text = (source / 'studio/install_python_stack.py').read_text(encoding="utf-8")
    begin = text.index('    # 8. Unsloth dependencies')
    end = text.index('    # 8b.', begin)
    code = compile(textwrap.dedent(text[begin:end]), 'actual-requirements-step', 'exec')
    calls = []
    original = stack.pip_install
    def install(*a, **kw):
        calls.append('install')
        return original(*a, **kw)
    def run_step(label, expected):
        plan()
        n = len(calls)
        exec(code, vars(stack))
        delta = len(calls) - n
        check(label + '_install_calls', delta, expected)
        imported = subprocess.run([sys.executable, '-c', 'import review_parent; import review_child; assert review_child.VALUE == 42'], capture_output=True, timeout=20)
        check(label + '_import', imported.returncode, 0)
        save_manifest()
        return delta
    with patch.object(im, 'requirements_root', lambda *a, **k: reqroot), patch.object(stack, 'pip_install', install):
        run_step('cold', 1)
        warm = run_step('warm', 0 if args.side == 'head' else 1)
        # Explicitly expose the invariant that should fail only on base.
        check('no_redundant_install_invariant', warm == 0, args.side == 'head', 'negative_proof')
        uv('uninstall', '--python', sys.executable, 'review_child')
        broken = subprocess.run([sys.executable, '-c', 'import review_parent'], capture_output=True, timeout=20)
        check('removed_child_breaks_import', broken.returncode != 0 and b'ModuleNotFoundError' in broken.stderr, True)
        run_step('repair', 1)
        run_step('settled_repair', 0 if args.side == 'head' else 1)
        constraints.write_text('review_child==2.0\n')
        run_step('constraint_change', 1)
        check('constraint_installed_version', metadata.version('review_child'), '2.0')
        run_step('settled_constraint', 0 if args.side == 'head' else 1)
        req.write_text('review_parent==1.0\n# input bytes changed\n')
        run_step('requirements_change', 1)
        run_step('settled_requirements', 0 if args.side == 'head' else 1)

def manifest_probe():
    other_spec = importlib.util.spec_from_file_location('other_manifest', Path(args.other) / 'studio/install_manifest.py')
    other = importlib.util.module_from_spec(other_spec)
    other_spec.loader.exec_module(other)
    with patch.object(im, 'requirements_root', lambda *a, **k: reqroot):
        save_manifest()
        check('other_reader_accepts_manifest', other.verify_install(req_root=reqroot, deep=True)['ok'], True)
        if args.side == 'head':
            old = im.read_manifest()
            old.pop('pass_inputs')
            im.manifest_path().write_text(json.dumps(old))
            check('legacy_manifest_forces_pass', plan() is None, True)
        for initial in ['live', 'parked']:
            save_manifest()
            if initial == 'parked': im.remove_manifest()
            class Interrupted(Exception): pass
            def interrupt(): raise Interrupted()
            with patch.object(stack, '_bootstrap_uv', interrupt):
                try: stack.install_python_stack()
                except Interrupted: pass
            check(initial + '_interrupted_is_incomplete', im.verify_install(req_root=reqroot)['ok'], False)
            if args.side == 'head':
                check(initial + '_interrupted_evidence_consumed', im.previous_manifest_path().exists(), False)
        if args.side == 'head':
            save_manifest()
            invoked = []
            with patch.object(im, 'consume_previous_manifest', lambda: None), patch.object(stack, '_bootstrap_uv', lambda: invoked.append(True)):
                rc = stack.install_python_stack()
            check('locked_parked_evidence_aborts', rc, 1)
            check('locked_parked_evidence_no_mutation', invoked, [])
            im.consume_previous_manifest()

def triton_probe():
    info, payload, record = fixture_dist('triton_kernels')
    url = 'https://example.invalid/triton.git'
    commit = 'a' * 40
    (info / 'direct_url.json').write_text(json.dumps({'url': url, 'subdirectory': 'python/triton_kernels', 'vcs_info': {'vcs': 'git', 'requested_revision': 'release/3.6.x', 'commit_id': commit}}))
    (reqroot / 'triton-kernels.txt').write_text(f'triton_kernels @ git+{url}@release/3.6.x#subdirectory=python/triton_kernels\n')
    tree = ast.parse((source / 'studio/install_python_stack.py').read_text(encoding="utf-8"))
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'install_python_stack')
    nodes = [n for n in function.body if isinstance(n, ast.If) and ('_triton_kernels_step()' in ast.unparse(n) or 'Installing triton kernels' in ast.unparse(n))]
    assert len(nodes) == 1
    code = compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), 'actual-triton-step', 'exec')
    for warm in [False, True]:
        for mode in ['intact', 'missing', 'truncated', 'no_record', 'forced']:
            payload.write_bytes(b'VALUE = 42\n')
            record.write_text('triton_kernels/__init__.py,,0\ntriton_kernels/internal.py,,11\n')
            if mode == 'missing': payload.unlink()
            if mode == 'truncated': payload.write_bytes(b'V')
            if mode == 'no_record': record.unlink()
            evidence = {'pass_inputs': im.pass_input_digests(reqroot), 'step_results': {'triton-kernels.txt': 'ran'}} if warm and args.side == 'head' else None
            calls = []
            with contextlib.ExitStack() as ctx:
                for name, value in [('IS_WINDOWS', False), ('IS_MACOS', False), ('IS_MAC_ARM', False), ('_PASS_EVIDENCE', evidence), ('_TOTAL', 100)]:
                    ctx.enter_context(patch.object(stack, name, value, create=True))
                ctx.enter_context(patch.object(stack, '_has_working_git', lambda: True))
                ctx.enter_context(patch.object(stack, 'pip_install', lambda *a, **k: calls.append(True)))
                if args.side == 'head': ctx.enter_context(patch.object(stack, '_git_remote_commit', lambda *a, **k: commit))
                ctx.enter_context(patch.dict(os.environ, {'UNSLOTH_STUDIO_FULL_DEPS': '1' if mode == 'forced' else '0'}))
                # Real startup rejects evidence for FULL_DEPS; model that handoff.
                if mode == 'forced': stack._PASS_EVIDENCE = None
                exec(code, vars(stack))
            expected = 0 if args.side == 'head' and mode == 'intact' else 1
            check(f'triton_{"warm" if warm else "no_evidence"}_{mode}_dispatch', len(calls), expected)

def mlx_probe():
    names = ('mlx', 'mlx-metal', 'mlx-lm', 'mlx-vlm', 'transformers', 'tokenizers', 'huggingface-hub', 'safetensors', 'numpy', 'pillow', 'protobuf', 'sentencepiece')
    fixtures = {name: fixture_dist(name) for name in names}
    for mode in ['unchanged', 'dependency_version', 'dependency_payload', 'mlx_payload', 'no_record']:
        for info, payload, record in fixtures.values():
            payload.write_bytes(b'VALUE = 42\n')
            module = payload.parent.name
            record.write_text(f'{module}/__init__.py,,0\n{module}/internal.py,,11\n')
        fingerprint = stack._mlx_health_fingerprint() if args.side == 'head' else {}
        evidence = {'mlx_health': dict(fingerprint, ok=True)}
        if mode == 'dependency_version' and args.side == 'head': evidence['mlx_health']['imports']['tokenizers'] = '0.0'
        if mode == 'dependency_payload': fixtures['tokenizers'][1].unlink()
        if mode == 'mlx_payload': fixtures['mlx-lm'][1].write_bytes(b'x')
        if mode == 'no_record': fixtures['mlx'][2].unlink()
        calls = []
        def run(cmd, **kw):
            assert cmd[2] == stack._MLX_HEALTH_PROBE
            calls.append('health_probe')
            return subprocess.CompletedProcess(cmd, 0, stdout='[]', stderr='')
        with patch.object(stack, '_PASS_EVIDENCE', evidence, create=True), patch.object(stack, '_INSTALL_ACTIONS', 0, create=True), patch.object(stack.subprocess, 'run', run):
            if args.side == 'head': stack._report_mlx_stack_health(skipped=True)
            else: stack._report_mlx_stack_health()
        check('mlx_' + mode + '_probe_calls', len(calls), 0 if args.side == 'head' and mode == 'unchanged' else 1)

def shell_probe():
    text = (source / 'studio/setup.sh').read_text(encoding="utf-8")
    start = text.index('        _setup_pin="${UNSLOTH_TORCH_INDEX_URL')
    end = text.index('    elif [ -n "$INSTALLED_VER"', start)
    block = work / 'fastpath.sh'
    block.write_text(text[start:end])
    cases = [('cpu','cu128',True), ('cu128','cpu',True), ('cu128','rocm7.1',True), ('rocm7.1','cu128',True),
             ('rocm7.1','cpu',True), ('cpu','rocm7.1',True), ('cu128','cu130',False), ('cpu','cu128-private',False), ('cpu','cpu',False)]
    for have, want, changed in cases:
        root = work / ('shell-' + have + '-' + want)
        version = root / 'lib/python3.13/site-packages/torch/version.py'
        version.parent.mkdir(parents=True, exist_ok=True)
        version.write_text(f"__version__ = '2.9.1+{have}'\n")
        env = dict(os.environ, VENV_DIR=str(root), UNSLOTH_TORCH_INDEX_URL='', UNSLOTH_TORCH_INDEX_FAMILY=want)
        run = subprocess.run(['bash','-c','_SKIP_PYTHON_DEPS=true; substep() { :; }; source "$1"; echo "$_SKIP_PYTHON_DEPS"','probe',str(block)], env=env, capture_output=True, text=True, timeout=20)
        assert run.returncode == 0, 'shell fixture did not execute'
        check('shell_' + have + '_to_' + want, run.stdout.strip(), 'false' if changed and args.side == 'head' else 'true')

def windows_probe():
    text = (source / 'studio/setup.ps1').read_text(encoding="utf-8")
    invoke = next(line.strip() for line in text.splitlines() if '$output = Fast-Install @_rocmTrio' in line and '$ROCmIndexUrl' in line)
    guard = ''
    if '$rocmForce = @()' in text:
        start = text.index('    $rocmForce = @()')
        guard = text[start:text.index('    while ($true)', start)]
    identity = '''function Get-IndexIdentity { param([string]$Url)
    $Url = $Url -replace '(https?://)[^/@\\s`]+@', '$1'
    return (($Url -split '[?#]', 2)[0]).TrimEnd('/')
}'''
    # Use the source function when present; base never used index identity.
    if 'function Get-IndexIdentity {' in text:
        start = text.index('function Get-IndexIdentity {')
        identity = text[start:text.index('\n}', start)+2]
    fixtures = {name: fixture_dist(name, '2.9.1+rocm7.1') for name in ['torchvision', 'torchaudio']}
    for mode in ['intact', 'wrong_family', 'missing_payload', 'no_record', 'changed_index']:
        for name, (info, payload, record) in fixtures.items():
            (info/'METADATA').write_text(f'Metadata-Version: 2.1\nName: {name}\nVersion: 2.9.1+rocm7.1\n')
            payload.write_bytes(b'VALUE = 42\n')
            record.write_text(f'{name}/__init__.py,,0\n{name}/internal.py,,11\n')
        if mode == 'wrong_family': (fixtures['torchvision'][0]/'METADATA').write_text('Metadata-Version: 2.1\nName: torchvision\nVersion: 2.9.1+cpu\n')
        if mode == 'missing_payload': fixtures['torchvision'][1].unlink()
        if mode == 'no_record': fixtures['torchvision'][2].unlink()
        rocmroot = work/'rocm'
        rocmroot.mkdir(exist_ok=True)
        (rocmroot/'.unsloth-rocm-index').write_text('https://example.invalid/old' if mode == 'changed_index' else 'https://example.invalid/gfx1201')
        ps = identity + '''
function substep { }
function Invoke-BoundedPythonProbe { param($PythonExe, $Code)
    $value = & $PythonExe -c $Code 2>&1
    return @{Ok=($LASTEXITCODE -eq 0); Output=($value | Out-String)}
}
function Fast-Install { $script:InstallArgs = @($args) }
$ROCmIndexUrl = 'https://example.invalid/gfx1201'
$installedTorchTag = 'rocm'
$script:PinChangedForceReinstall = $false
$script:TorchImportDefinitivelyFailed = $false
$VenvDir = $env:PROBE_ROOT
$VenvPyExe = $env:PROBE_PYTHON
$_rocmTrio = @('torch', 'torchvision', 'torchaudio')
''' + guard + '\n' + invoke + '\nWrite-Output ("FORCE=" + ($script:InstallArgs -contains "--force-reinstall"))\n'
        script = work/'rocm.ps1'
        script.write_text(ps)
        run = subprocess.run(['pwsh','-NoProfile','-File',str(script)], env=dict(os.environ, PROBE_ROOT=str(rocmroot), PROBE_PYTHON=sys.executable), capture_output=True, text=True, timeout=30)
        assert run.returncode == 0 and 'FORCE=' in run.stdout, 'PowerShell guard fixture did not execute'
        check('windows_rocm_' + mode + '_force', 'FORCE=True' in run.stdout, args.side == 'base' or mode != 'intact')

def plugin_probe():
    tree = work / 'plugin'
    tree.mkdir()
    module = tree / 'plugin.py'
    module.write_text('VALUE = 1\n')
    before = stack._local_plugin_digest(tree) if hasattr(stack, '_local_plugin_digest') else None
    if before is None:
        check('plugin_digest_available', False, args.side == 'head')
        return
    for artifact in ['build/lib/plugin.py', 'dist/output.whl', 'src/plugin.egg-info/PKG-INFO', '__pycache__/plugin.pyc']:
        target = tree / artifact
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('generated')
        check('plugin_ignores_' + artifact, stack._local_plugin_digest(tree), before)
    module.write_text('VALUE = 2\n')
    check('plugin_source_edit_invalidates', stack._local_plugin_digest(tree) != before, True)

phase('plugin', plugin_probe)
phase('requirements', requirements_probe)
phase('manifest', manifest_probe)
phase('triton', triton_probe)
if sys.platform == 'darwin' or args.all_python_platforms: phase('mlx', mlx_probe)
if sys.platform == 'win32': phase('windows_rocm', windows_probe)
if sys.platform.startswith('linux'): phase('shell', shell_probe)
sha = subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'], text=True).strip()
payload = {'side': args.side, 'source_sha': sha, 'python': platform.python_version(), 'os': sys.platform,
           'machine': platform.machine(), 'results': results,
           'limitations': ['Fixture packages, not a full Studio installation', 'No GPU kernels or browser tests', 'MLX import subprocess and git remote responses are controlled test doubles']}
(out / (args.side + '.json')).write_text(json.dumps(payload, indent=2)+'\n')
sys.exit(1 if any(r['status'] != 'pass' for r in results) else 0)
