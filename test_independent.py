# SPDX-License-Identifier: AGPL-3.0-only
"""Independent decision simulations; mocked platforms are not native hardware tests."""
import json
import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(os.environ.get('PR10649_CHECKOUT_ROOT', str(Path(__file__).resolve().parent))) / 'head'
sys.path.insert(0, str(ROOT / 'tests/studio/install'))
from test_dependency_pass_skips import stack, manifest


@pytest.mark.parametrize('writer_name', ['base', 'head'])
@pytest.mark.parametrize('reader_name', ['base', 'head'])
def test_real_manifest_old_new_readers(monkeypatch, tmp_path, writer_name, reader_name):
    def load(name):
        source = ROOT.parent / name / 'studio/install_manifest.py'
        spec = importlib.util.spec_from_file_location('compat_manifest_' + name, source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    writer, reader = load(writer_name), load(reader_name)
    monkeypatch.setattr(writer, '_installed_version', lambda *a, **k: '1.0')
    req_root = tmp_path / 'requirements'
    req_root.mkdir()
    (req_root / 'studio.txt').write_text('')
    root = tmp_path / 'venv'
    root.mkdir()
    extra = {'extra': {'pass_inputs': {'studio.txt': 'hash'}, 'step_results': {'studio.txt': 'ran'}}} if writer_name == 'head' else {}
    assert writer.write_manifest(root=root, req_root=req_root, **extra)
    result = reader.verify_install(root=root, req_root=req_root, installed={'unsloth': '1.0', 'unsloth-zoo': '1.0'})
    assert result['ok'], result
    assert writer.remove_manifest(root)
    result = reader.verify_install(root=root, req_root=req_root, installed={'unsloth': '1.0'})
    assert not result['ok'] and result['reason'] == 'studio_install_incomplete'


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    for key in ('_PASS_EVIDENCE', '_CONSTRAINTS_CACHE', '_CLOSURE_INDEX_CACHE'):
        monkeypatch.setattr(stack, key, None)
    monkeypatch.setattr(stack, '_INSTALL_ACTIONS', 0)
    stack._STEP_RESULTS.clear()
    stack._AUDITED_STEPS.clear()
    for key in (*stack._FOREIGN_RESOLVER_ENV, 'UV_OVERRIDE', 'UNSLOTH_STUDIO_FULL_DEPS',
                'STUDIO_LOCAL_REPO', 'STUDIO_PACKAGE_NAME', 'UNSLOTH_CI_SOURCE_OVERLAY', 'SKIP_STUDIO_BASE'):
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize('host', ['Windows', 'Linux', 'WSL', 'Mac'])
@pytest.mark.parametrize('device', ['NVIDIA', 'AMD', 'CPU'])
@pytest.mark.parametrize('change', ['none', 'python', 'abi', 'platform', 'flavor', 'no_torch', 'forced', 'legacy', 'failed_verify'])
def test_evidence_matrix(monkeypatch, manifest, host, device, change):
    payload, _ = manifest
    # This tests the cross-platform evidence contract, not wheel availability or GPU execution.
    host_platform = {'Windows': 'win32', 'Linux': 'linux', 'WSL': 'linux', 'Mac': 'darwin'}[host]
    machine = 'arm64' if host == 'Mac' else 'x86_64'
    flavor = {'NVIDIA': 'cu128', 'AMD': 'rocm7.2', 'CPU': 'cpu'}[device]
    monkeypatch.setattr(stack.sys, 'platform', host_platform)
    monkeypatch.setattr(stack.platform, 'machine', lambda: machine)
    monkeypatch.setattr(stack, '_expected_torch_flavor_tag', lambda: flavor)
    payload['platform'] = host_platform + '-' + machine
    payload['expected_torch_tag'] = flavor
    if change == 'python': payload['python'] = '3.1.0'
    if change == 'abi': payload['installer_python_tag'] += 't'
    if change == 'platform': payload['platform'] = 'other-platform'
    if change == 'flavor': payload['expected_torch_tag'] = 'other-flavor'
    if change == 'no_torch': payload['no_torch'] = True
    if change == 'forced': monkeypatch.setenv('UNSLOTH_STUDIO_FULL_DEPS', '1')
    if change == 'legacy': payload.pop('pass_inputs')
    if change == 'failed_verify':
        monkeypatch.setattr(stack.install_manifest, 'verify_install', lambda **k: {'ok': False})
    assert (stack._plan_pass('unsloth', '', '') is not None) == (change == 'none')


def test_interrupted_live_manifest_does_not_reauthorize_evidence(monkeypatch, manifest, tmp_path):
    payload, _ = manifest
    live = tmp_path / 'live.json'
    previous = tmp_path / 'previous.json'
    live.write_text(json.dumps(payload))
    monkeypatch.setattr(stack.install_manifest, 'manifest_path', lambda root=None: live)
    monkeypatch.setattr(stack.install_manifest, 'previous_manifest_path', lambda root=None: previous)
    def read(path):
        return json.loads(path.read_text()) if path.exists() else None
    monkeypatch.setattr(stack.install_manifest, 'read_manifest', lambda root=None: read(live))
    monkeypatch.setattr(stack.install_manifest, 'read_previous_manifest', lambda root=None: read(previous))
    monkeypatch.setattr(stack.install_manifest, 'set_no_torch_marker', lambda *a: None)
    # Interrupt the real installer immediately after its manifest-removal handoff.
    def interrupted():
        raise KeyboardInterrupt('simulated process interruption')
    monkeypatch.setattr(stack, '_bootstrap_uv', interrupted)
    with pytest.raises(KeyboardInterrupt):
        stack.install_python_stack()
    assert not live.exists()
    assert stack._plan_pass('unsloth', '', '') is None, 'interrupted pass reused parked evidence'


@pytest.mark.parametrize('forced', [False, True])
def test_triton_missing_payload_reaches_install(monkeypatch, tmp_path, forced):
    req = tmp_path / 'triton-kernels.txt'
    revision = 'release/3.6.x'
    url = 'https://example.invalid/triton.git'
    req.write_text(f'triton_kernels @ git+{url}@{revision}#subdirectory=python/triton_kernels\n')
    # Real on-disk metadata left after the package payload has been deleted.
    site = tmp_path / 'site'
    dist = site / 'triton_kernels-1.0.dist-info'
    dist.mkdir(parents=True)
    (dist / 'METADATA').write_text('Metadata-Version: 2.1\nName: triton_kernels\nVersion: 1.0\n')
    (dist / 'RECORD').write_text('triton_kernels/__init__.py,,20\n')
    (dist / 'direct_url.json').write_text(json.dumps({
        'url': url, 'subdirectory': 'python/triton_kernels',
        'vcs_info': {'vcs': 'git', 'requested_revision': revision, 'commit_id': 'a'*40}}))
    monkeypatch.syspath_prepend(str(site))
    monkeypatch.setattr(stack, 'REQ_ROOT', tmp_path)
    monkeypatch.setattr(stack, '_has_working_git', lambda: True)
    monkeypatch.setattr(stack, '_git_remote_commit', lambda *a, **k: 'a'*40)
    if forced: monkeypatch.setenv('UNSLOTH_STUDIO_FULL_DEPS', '1')
    installs = []
    monkeypatch.setattr(stack, 'pip_install', lambda *a, **k: installs.append((a,k)))
    stack._triton_kernels_step()
    assert installs, 'missing Triton payload was treated as an installed build'
