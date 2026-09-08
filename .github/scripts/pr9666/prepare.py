# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.
"""Add a test-only native binary and production-component scene to a disposable checkout."""
import hashlib
import json
import pathlib
import re
import shutil
import subprocess
import uuid

PACKAGE = pathlib.Path(__file__).resolve().parent
MANIFEST = json.loads((PACKAGE / 'manifest.json').read_text())

def extract(source, pattern):
    matches = re.findall(pattern, source, re.MULTILINE | re.DOTALL)
    if len(matches) != 1:
        raise ValueError(f'Expected exactly one production block, found {len(matches)}')
    return matches[0]

def prepare(root, side, port, output, run_id):
    root, output = pathlib.Path(root).resolve(), pathlib.Path(output).resolve()
    sha = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    if sha != MANIFEST[side]:
        raise ValueError(f'{side} SHA mismatch')
    front, native = root / 'studio/frontend', root / 'studio/src-tauri'
    provider = (front / 'src/app/provider.tsx').read_text()
    style = extract(provider, r'(const MAC_NATIVE_CHROME_STYLE = \{.*?^\} as CSSProperties;)')
    effect = extract(provider, r'(function AppearanceCustomizationEffect\(\) \{.*?^\})')
    imports, boot = 'const isTauri = true;', ''
    if side == 'head':
        imports += '''
import {applyInterfaceScale,applyInterfaceScaleBeforeFirstPaint,useInterfaceScaleStore} from './src/features/settings/stores/interface-scale-store';
import {NATIVE_MAC_TITLEBAR_HEIGHT_VAR,NATIVE_MAC_TRAFFIC_LIGHT_INSET_VAR} from './src/features/settings/lib/interface-scale-runtime';
'''
        boot = 'await applyInterfaceScaleBeforeFirstPaint(useInterfaceScaleStore.getState().scale);'
    scene = (PACKAGE / 'scene.tsx').read_text()
    replacements = {
        '/* SIDE_IMPORTS */': imports,
        '/* SIDE_MANIFEST */': json.dumps({'side': side, 'sha': sha}),
        '/* PRODUCTION_MAC_STYLE */': style,
        '/* PRODUCTION_EFFECT */': effect,
        '/* PRODUCTION_SCALE_BOOT */': boot,
    }
    for marker, value in replacements.items():
        if scene.count(marker) != 1: raise ValueError(f'Marker mismatch: {marker}')
        scene = scene.replace(marker, value)
    (front / 'pr9666-native.tsx').write_text(scene)
    (front / 'pr9666-native.html').write_text('<!doctype html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="root"></div><script src="/crypto-boot.js"></script><script type="module" src="/pr9666-native.tsx"></script></body></html>')
    bins = native / 'src/bin'; bins.mkdir(exist_ok=True)
    shutil.copyfile(PACKAGE / 'probe.rs', bins / 'pr9666_probe.rs')
    shutil.copyfile(PACKAGE / 'native.m', native / 'pr9666_native.m')
    original_build = subprocess.check_output(['git','-C',str(root),'show','HEAD:studio/src-tauri/build.rs'],text=True)
    if original_build.count('fn main()') != 1: raise ValueError('Unrecognized build script')
    # No new Cargo dependencies or lockfile changes: compile a tiny ObjC bridge
    # against the runner's SDK and link it only into the disposable probe binary.
    addition = '''
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("macos") {
        let out = std::path::PathBuf::from(std::env::var("OUT_DIR").unwrap());
        let object = out.join("pr9666_native.o");
        assert!(std::process::Command::new("xcrun").args(["clang", "-fobjc-arc", "-c", "pr9666_native.m", "-o"]).arg(&object).status().unwrap().success());
        let archive = out.join("libpr9666_native.a");
        assert!(std::process::Command::new("ar").arg("rcs").arg(archive).arg(object).status().unwrap().success());
        println!("cargo:rustc-link-search=native={}", out.display());
        println!("cargo:rustc-link-arg-bin=pr9666_probe=-lpr9666_native");
        println!("cargo:rerun-if-changed=pr9666_native.m");
    }
'''
    build = original_build.replace('fn main() {', 'fn main() {' + addition)
    if build == original_build: raise ValueError('Build instrumentation failed')
    (native / 'build.rs').write_text(build)
    config = json.loads((native / 'tauri.conf.json').read_text())
    window = dict(config['app']['windows'][0])
    profile = uuid.uuid5(uuid.NAMESPACE_URL, f'pr9666/{run_id}/{side}/{sha}')
    identity = {'side':side,'sha':sha,'profile_uuid':str(profile)}
    (front/'public/pr9666-identity.json').write_text(json.dumps(identity))
    window.update(width=900, height=600, visible=False, resizable=True,
                  dataStoreIdentifier=list(profile.bytes))
    override = {'identifier': f'ai.unsloth.pr9666.{side}',
                'build': {'devUrl': f'http://127.0.0.1:{port}/pr9666-native.html', 'beforeDevCommand': None},
                'app': {'windows': [window]}}
    output.mkdir(parents=True, exist_ok=True)
    provenance = {'side': side, 'sha': sha, 'source_root': str(root), 'port': port,
                  'profile_uuid': str(profile), 'run_id': run_id, 'scene': MANIFEST['scene'],
                  'evidence_scope': MANIFEST['evidence_scope'],
                  'original_build_sha256': hashlib.sha256(original_build.encode()).hexdigest(),
                  'scene_sha256': hashlib.sha256(scene.encode()).hexdigest(),
                  'mac_style': style, 'appearance_effect': effect,
                  'config_override': override}
    (output / 'provenance.json').write_text(json.dumps(provenance, indent=2))
    return override
