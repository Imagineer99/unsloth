# SPDX-License-Identifier: AGPL-3.0-only
import sys, importlib.util, json, dataclasses
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parent
sys.path[:0]=[str(ROOT/'head/tests/_shared'),str(ROOT/'head/tests/studio/install')]
import test_install_llama_prebuilt_logic as H
M=H.INSTALL_LLAMA_PREBUILT

def test_latest_pointer_must_not_hide_newer_publication(monkeypatch):
    releases=[{'tag_name':'release-1','published_at':'2026-01-01T00:00:00Z'}, {'tag_name':'release-2','published_at':'2026-02-01T00:00:00Z'}]
    monkeypatch.setattr(M,'github_releases',lambda *a,**kw:releases)
    monkeypatch.setattr(M,'_download_host_latest_release_tag',lambda *a:'release-1')
    monkeypatch.setattr(M,'_METADATA_MEMO',None)
    monkeypatch.delenv('UNSLOTH_LLAMA_DISABLE_DOWNLOAD_HOST_RESOLVE',raising=False)
    host=H.macos_host(macos_version=(15,0))
    full=next(M.iter_release_payloads_by_time(M.DEFAULT_PUBLISHED_REPO,requested_tag='latest'))['tag_name']
    fast=M._expected_release_tag_without_plan({'release_tag':'release-1','tag':'b9001'},'latest',M.DEFAULT_PUBLISHED_REPO,'',host=host)
    assert fast==full, f'fast path keeps {fast}; full publication ordering selects {full}'

@pytest.mark.parametrize('osname',['Windows','Linux','WSL','Mac'])
@pytest.mark.parametrize('gpu',['NVIDIA','AMD','CPU'])
def test_profile_roundtrip_and_hardware_change(osname,gpu,monkeypatch):
    factory={'Windows':H.windows_host,'Linux':H.linux_host,'WSL':H.linux_host,'Mac':H.macos_host}[osname]
    fields={}
    if gpu=='NVIDIA': fields.update(H._CUDA_HOST_FIELDS)
    if gpu=='AMD': fields.update(has_rocm=osname!='Mac',has_amd_gpu_without_rocm=osname=='Mac',rocm_gfx_target='gfx1100',rocm_gfx_targets=['gfx1100'])
    host=factory(**fields)
    monkeypatch.setattr(M,'_detect_host_rocm_version',lambda:(6,4))
    monkeypatch.setattr(M,'detected_linux_runtime_lines',lambda:(['cuda12'],{}))
    monkeypatch.setattr(M,'detected_windows_runtime_lines',lambda:(['cuda12'],{}))
    before=M.host_profile(host)
    assert json.loads(json.dumps(before))==before
    assert M.host_profile(dataclasses.replace(host,machine='different-architecture'))!=before
    assert M.host_profile(dataclasses.replace(host,has_physical_nvidia=not host.has_physical_nvidia))!=before
